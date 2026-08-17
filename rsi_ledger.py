"""vs-sp500: RSI-30枠の台帳I/O（ledger/rsi/ 配下）。

本体のportfolio.pyとは別ファイル・別スキーマで完全に独立させる
（本体の台帳・関数には一切触れない。SPEC_RSI30.md「本体を壊さないこと」）。
"""
from __future__ import annotations

import csv
import json
from typing import Any

import config
from market import TickerSnapshot

DEFAULT_RSI_PORTFOLIO: dict[str, Any] = {
    "start_date": None,
    "cash_usd": config.RSI_INITIAL_CAPITAL_USD,
    "lots": [],
    "bench_units_rsi": 0.0,
    "last_processed_date": None,
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
