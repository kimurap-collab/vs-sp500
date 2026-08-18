"""vs-sp500: RSI-30枠の台帳I/O（ledger/rsi/ 配下）。

本体のportfolio.pyとは別ファイル・別スキーマで完全に独立させる
（本体の台帳・関数には一切触れない。SPEC_RSI30.md「本体を壊さないこと」）。
"""
from __future__ import annotations

import csv
import json
import logging
from typing import Any

import config
from market import TickerSnapshot

logger = logging.getLogger("vs-sp500.rsi_ledger")

DEFAULT_RSI_PORTFOLIO: dict[str, Any] = {
    "start_date": None,
    "cash_usd": config.RSI_INITIAL_CAPITAL_USD,
    "lots": [],
    "bench_units_rsi": 0.0,
    "last_processed_date": None,
    "pending_orders": [],
}

RSI_TRADES_CSV_HEADER = ["date", "action", "ticker", "shares", "price", "amount_usd", "rule", "lot_id", "note"]
RSI_HISTORY_CSV_HEADER = ["date", "nav_usd", "bench_usd", "diff_usd", "diff_pct", "cash_ratio", "open_lots"]


def load_portfolio() -> dict[str, Any]:
    if not config.RSI_PORTFOLIO_PATH.exists():
        return json.loads(json.dumps(DEFAULT_RSI_PORTFOLIO))
    with open(config.RSI_PORTFOLIO_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_portfolio(state: dict[str, Any]) -> None:
    config.RSI_LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = config.RSI_PORTFOLIO_PATH.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp_path.replace(config.RSI_PORTFOLIO_PATH)


def append_trade_row(row: dict[str, Any]) -> None:
    config.RSI_LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    is_new = not config.RSI_TRADES_CSV_PATH.exists()
    with open(config.RSI_TRADES_CSV_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=RSI_TRADES_CSV_HEADER)
        if is_new:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in RSI_TRADES_CSV_HEADER})


def append_history_row(row: dict[str, Any]) -> None:
    """同日の行があれば上書き、無ければ追記する（本体のappend_history_rowと同じ規約）。"""
    config.RSI_LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    if config.RSI_HISTORY_CSV_PATH.exists():
        with open(config.RSI_HISTORY_CSV_PATH, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    rows = [r for r in rows if r.get("date") != row["date"]]
    rows.append({k: row.get(k, "") for k in RSI_HISTORY_CSV_HEADER})
    rows.sort(key=lambda r: r["date"])
    with open(config.RSI_HISTORY_CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=RSI_HISTORY_CSV_HEADER)
        writer.writeheader()
        writer.writerows(rows)


def read_history_rows() -> list[dict[str, Any]]:
    if not config.RSI_HISTORY_CSV_PATH.exists():
        return []
    with open(config.RSI_HISTORY_CSV_PATH, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def read_trade_rows() -> list[dict[str, Any]]:
    if not config.RSI_TRADES_CSV_PATH.exists():
        return []
    with open(config.RSI_TRADES_CSV_PATH, encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# 集計
# ---------------------------------------------------------------------------

def open_lots(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [lot for lot in state.get("lots", []) if not lot.get("closed")]


def holdings_by_ticker(state: dict[str, Any]) -> dict[str, float]:
    """全ロット（クローズ済み除く）の銘柄別合計株数。"""
    holdings: dict[str, float] = {}
    for lot in open_lots(state):
        holdings[lot["ticker"]] = holdings.get(lot["ticker"], 0.0) + lot["shares"]
    return holdings


def compute_nav_usd(state: dict[str, Any], market: dict[str, TickerSnapshot]) -> float:
    nav = state["cash_usd"]
    for ticker, shares in holdings_by_ticker(state).items():
        snap = market.get(ticker)
        if snap is None:
            continue
        nav += shares * snap.close
    return nav


def compute_bench_nav_usd(state: dict[str, Any], voo_close: float) -> float:
    return state["bench_units_rsi"] * voo_close


def compute_cash_ratio(state: dict[str, Any], nav_usd: float) -> float:
    if nav_usd <= 0:
        return 0.0
    return state["cash_usd"] / nav_usd


def compute_available_cash(state: dict[str, Any], market_prices: dict[str, float] | None = None) -> float:
    """新規注文の可否判断・数量計算に使う現金（RSI枠専用。2026-08-18 修正1）。

    台帳のcash_usdから、RSI枠自身の未決BUY注文の額面合計（未約定残数×発注時点の想定単価
    est_price）を差し引く。本体のpending_ordersは見ない（互いの注文を見ない原則を崩さない）。
    est_price未記録の旧pendingはmarket_pricesの現在値で代用する。代用先も無ければ予約額0とする。
    """
    reserved = 0.0
    for order in state.get("pending_orders", []):
        if order.get("side") != "BUY":
            continue
        remaining_qty = order["qty"] - order.get("applied_qty", 0)
        if remaining_qty <= 0:
            continue
        est_price = order.get("est_price")
        if est_price is None:
            est_price = (market_prices or {}).get(order["ticker"])
            if est_price is None:
                logger.warning(
                    "pending_orders(order_id=%s, %s)にest_priceが無く現在値も取得できないため、"
                    "現金予約額0として扱う", order.get("order_id"), order.get("ticker"),
                )
                est_price = 0.0
        reserved += remaining_qty * est_price
    return state["cash_usd"] - reserved
