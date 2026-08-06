"""vs-sp500: data.json生成・Telegram送信。"""
from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

import requests

import config
from market import TickerSnapshot

logger = logging.getLogger("vs-sp500.report")

THESES_PATH = config.BASE_DIR / "theses.json"


def _load_theses() -> dict[str, Any]:
    """theses.json（銘柄テーゼ・Fable管理）を読み込む。無ければ空辞書。"""
    if not THESES_PATH.exists():
        return {}
    try:
        with open(THESES_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if not k.startswith("_")}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("theses.json読み込み失敗: %s", e)
        return {}


def _ticker_link(ticker: str, currency: str) -> str:
    if currency == "JPY":
        return f"https://finance.yahoo.co.jp/quote/{ticker}"
    return f"https://finance.yahoo.com/quote/{ticker}"


def build_data_json(
    state: dict[str, Any],
    market: dict[str, TickerSnapshot],
    usdjpy_mid: float,
    nav_jpy: float,
    bench_jpy: float,
    accepted_trades: list[dict[str, Any]],
    now_jst: dt.datetime,
) -> dict[str, Any]:
    diff_jpy = nav_jpy - bench_jpy
    diff_pct = (diff_jpy / bench_jpy * 100.0) if bench_jpy else 0.0

    from portfolio import compute_avg_costs, read_history_rows, read_trade_rows

    avg_costs = compute_avg_costs()
    theses = _load_theses()

    holdings = []
    for ticker, shares in state.get("holdings", {}).items():
        snap = market.get(ticker)
        if snap is None:
            continue
        currency = config.WHITELIST[ticker]["currency"]
        value_jpy = shares * snap.close * (usdjpy_mid if currency == "USD" else 1.0)
        weight_pct = (value_jpy / nav_jpy * 100.0) if nav_jpy else 0.0
        thesis = theses.get(ticker, {})
        holdings.append({
            "ticker": ticker,
            "name": config.WHITELIST[ticker]["name"],
            "shares": round(shares, 4),
            "value_jpy": round(value_jpy),
            "weight_pct": round(weight_pct, 2),
            "price": round(snap.close, 4),
            "avg_cost": round(avg_costs.get(ticker, snap.close), 4),
            "currency": currency,
            "link": _ticker_link(ticker, currency),
            "reason": thesis.get("reason", ""),
            "target": thesis.get("target", ""),
        })

    history_rows = read_history_rows()
    history = [{"date": r["date"], "nav": round(float(r["nav_jpy"])), "bench": round(float(r["bench_jpy"]))}
               for r in history_rows]

    trade_rows = read_trade_rows()
    trades_recent = trade_rows[-config.DATA_JSON_TRADES_LIMIT:]
    trades_recent.reverse()

    monthly = _build_monthly_table(history_rows)

    return {
        "updated_at": now_jst.strftime("%Y-%m-%d %H:%M JST"),
        "start_date": state.get("start_date"),
        "nav_jpy": round(nav_jpy),
        "bench_jpy": round(bench_jpy),
        "diff_jpy": round(diff_jpy),
        "diff_pct": round(diff_pct, 2),
        "mode": state.get("mode"),
        "holdings": holdings,
        "cash_jpy": round(state.get("cash_jpy", 0)),
        "cash_usd": round(state.get("cash_usd", 0.0), 2),
        "history": history,
        "trades": trades_recent,
        "monthly": monthly,
    }


def _build_monthly_table(history_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """月末（各月の最終行）時点のnav/benchで月次勝敗をまとめる。"""
    by_month: dict[str, dict[str, Any]] = {}
    for row in history_rows:
        month = row["date"][:7]
        by_month[month] = row  # 日付昇順で読んでいるため最後の代入が月末値になる
    monthly = []
    for month, row in sorted(by_month.items()):
        nav = float(row["nav_jpy"])
        bench = float(row["bench_jpy"])
        monthly.append({
            "month": month,
            "nav": round(nav),
            "bench": round(bench),
            "result": "win" if nav >= bench else "lose",
        })
    return monthly


def save_data_json(data: dict[str, Any]) -> None:
    with open(config.DATA_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def build_telegram_message(
    data: dict[str, Any],
    accepted_trades: list[dict[str, Any]],
    now_jst: dt.datetime,
    prev_month_result_line: str | None = None,
) -> str:
    date_str = f"{now_jst.month}/{now_jst.day}"
    diff_jpy = data["diff_jpy"]
    diff_pct = data["diff_pct"]
    emoji = "🟢" if diff_jpy >= 0 else "🔴"
    sign = "+" if diff_jpy >= 0 else ""

    if accepted_trades:
        trade_lines = "\n".join(
            f"・{t['action']} {t['ticker']} {t['amount_jpy']:,}円（{t['rule']}）" for t in accepted_trades
        )
        trade_block = f"売買:\n{trade_lines}"
    else:
        trade_block = "売買: なし（ホールド）"

    lines = [f"📊 vs S&P500 ({date_str})"]
    if prev_month_result_line:
        lines.append(prev_month_result_line)
    lines.append(f"評価額: ¥{data['nav_jpy']:,}")
    lines.append(f"対S&P: {sign}{diff_jpy:,}円 ({sign}{diff_pct:.2f}%) {emoji}")
    lines.append(trade_block)
    lines.append(f"📈 {config.GITHUB_PAGES_URL}")
    return "\n".join(lines)


def send_telegram_message(text: str) -> bool:
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        logger.error("Telegram設定が不足しているため送信できない")
        return False
    try:
        resp = requests.post(
            config.TELEGRAM_API_URL,
            json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text},
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        logger.error("Telegram送信失敗: %s", e)
        return False
