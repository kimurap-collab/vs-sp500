"""vs-sp500: 日本株RSI枠の台帳I/O（ledger/rsi_jp/ 配下）。

rsi_ledger.py（米国RSI-32枠）と構造は倣うが、ベンチマーク無し（大将「戦わなくていい。
元本からどれだけ増えたかだけでしい」）のためbench_units相当は持たない。
本体・米国RSI枠の台帳・関数には一切触れない。
"""
from __future__ import annotations

import csv
import json
import logging
from typing import Any

import config
from jp_market import JpSnapshot

logger = logging.getLogger("vs-sp500.jp_rsi_ledger")

DEFAULT_JP_PORTFOLIO: dict[str, Any] = {
    "start_date": None,
    "cash_jpy": config.RSI_JP_INITIAL_CAPITAL_JPY,
    "lots": [],
    "last_processed_date": None,
}

JP_TRADES_CSV_HEADER = ["date", "action", "ticker", "shares", "price", "amount_jpy", "rule", "lot_id", "note", "name"]
JP_HISTORY_CSV_HEADER = ["date", "nav_jpy", "principal_jpy", "diff_jpy", "diff_pct", "cash_ratio", "open_lots"]


def load_portfolio() -> dict[str, Any]:
    if not config.RSI_JP_PORTFOLIO_PATH.exists():
        return json.loads(json.dumps(DEFAULT_JP_PORTFOLIO))
    with open(config.RSI_JP_PORTFOLIO_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_portfolio(state: dict[str, Any]) -> None:
    config.RSI_JP_LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = config.RSI_JP_PORTFOLIO_PATH.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp_path.replace(config.RSI_JP_PORTFOLIO_PATH)


def append_trade_row(row: dict[str, Any]) -> None:
    config.RSI_JP_LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    is_new = not config.RSI_JP_TRADES_CSV_PATH.exists()
    with open(config.RSI_JP_TRADES_CSV_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=JP_TRADES_CSV_HEADER)
        if is_new:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in JP_TRADES_CSV_HEADER})


def append_history_row(row: dict[str, Any]) -> None:
    """同日の行があれば上書き、無ければ追記する（本体・米国RSI枠と同じ規約）。"""
    config.RSI_JP_LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    if config.RSI_JP_HISTORY_CSV_PATH.exists():
        with open(config.RSI_JP_HISTORY_CSV_PATH, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    rows = [r for r in rows if r.get("date") != row["date"]]
    rows.append({k: row.get(k, "") for k in JP_HISTORY_CSV_HEADER})
    rows.sort(key=lambda r: r["date"])
    with open(config.RSI_JP_HISTORY_CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=JP_HISTORY_CSV_HEADER)
        writer.writeheader()
        writer.writerows(rows)


def read_history_rows() -> list[dict[str, Any]]:
    if not config.RSI_JP_HISTORY_CSV_PATH.exists():
        return []
    with open(config.RSI_JP_HISTORY_CSV_PATH, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def read_trade_rows() -> list[dict[str, Any]]:
    if not config.RSI_JP_TRADES_CSV_PATH.exists():
        return []
    with open(config.RSI_JP_TRADES_CSV_PATH, encoding="utf-8") as f:
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


def compute_nav_jpy(state: dict[str, Any], market: dict[str, JpSnapshot]) -> float:
    nav = state["cash_jpy"]
    for ticker, shares in holdings_by_ticker(state).items():
        snap = market.get(ticker)
        if snap is None:
            continue
        nav += shares * snap.close
    return nav


def compute_cash_ratio(state: dict[str, Any], nav_jpy: float) -> float:
    if nav_jpy <= 0:
        return 0.0
    return state["cash_jpy"] / nav_jpy
