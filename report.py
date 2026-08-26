"""vs-sp500: data.json生成・Telegram送信。"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
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


def build_rsi_block(
    rsi_state: dict[str, Any],
    rsi_market: dict[str, TickerSnapshot],
    rsi_nav_usd: float,
    rsi_bench_usd: float,
    rsi_accepted_trades: list[dict[str, Any]],
) -> dict[str, Any]:
    """RSI-30枠のdata.json用ブロックを組み立てる（本体のbuild_data_jsonと対になる関数）。

    ロットは同一銘柄で複数存在しうるため、管理画面向けにはロット単位ではなく
    銘柄ごとに合算して表示する（保有株数・評価額の合計、平均取得単価は総投入額÷総株数）。
    """
    import rsi_ledger

    diff_usd = rsi_nav_usd - rsi_bench_usd
    diff_pct = (diff_usd / rsi_bench_usd * 100.0) if rsi_bench_usd else 0.0
    principal_usd = config.RSI_INITIAL_CAPITAL_USD
    principal_diff_usd = rsi_nav_usd - principal_usd
    principal_diff_pct = (principal_diff_usd / principal_usd * 100.0) if principal_usd else 0.0

    by_ticker: dict[str, dict[str, Any]] = {}
    for lot in rsi_ledger.open_lots(rsi_state):
        t = lot["ticker"]
        agg = by_ticker.setdefault(t, {"shares": 0.0, "invested": 0.0, "name": lot.get("name")})
        agg["shares"] += lot["shares"]
        agg["invested"] += lot["shares"] * lot["avg_cost"]

    holdings = []
    for ticker, agg in by_ticker.items():
        snap = rsi_market.get(ticker)
        if snap is None or agg["shares"] <= 0:
            continue
        value_usd = agg["shares"] * snap.close
        weight_pct = (value_usd / rsi_nav_usd * 100.0) if rsi_nav_usd else 0.0
        holdings.append({
            "ticker": ticker,
            "name": agg.get("name"),
            "shares": round(agg["shares"], 4),
            "value_usd": round(value_usd, 2),
            "weight_pct": round(weight_pct, 2),
            "price": round(snap.close, 4),
            "avg_cost": round(agg["invested"] / agg["shares"], 4),
            "link": _ticker_link(ticker, "USD"),
        })
    holdings.sort(key=lambda h: -h["value_usd"])

    history_rows = rsi_ledger.read_history_rows()
    history = [{"date": r["date"], "nav": round(float(r["nav_usd"]), 2), "bench": round(float(r["bench_usd"]), 2)}
               for r in history_rows]

    trade_rows = rsi_ledger.read_trade_rows()
    trades_recent = trade_rows[-config.DATA_JSON_TRADES_LIMIT:]
    trades_recent.reverse()

    monthly = _build_monthly_table(history_rows)

    return {
        "start_date": rsi_state.get("start_date"),
        "nav_usd": round(rsi_nav_usd, 2),
        "bench_usd": round(rsi_bench_usd, 2),
        "diff_usd": round(diff_usd, 2),
        "diff_pct": round(diff_pct, 2),
        "principal_usd": round(principal_usd, 2),
        "principal_diff_usd": round(principal_diff_usd, 2),
        "principal_diff_pct": round(principal_diff_pct, 2),
        "cash_usd": round(rsi_state.get("cash_usd", 0.0), 2),
        "open_lots": len(rsi_ledger.open_lots(rsi_state)),
        "holdings": holdings,
        "history": history,
        "trades": trades_recent,
        "monthly": monthly,
    }


def build_rsi_jp_block(
    jp_state: dict[str, Any],
    jp_market_snap: dict[str, Any],
    jp_nav_jpy: float,
    jp_accepted_trades: list[dict[str, Any]],
) -> dict[str, Any]:
    """日本株RSI枠のdata.json用ブロックを組み立てる（build_rsi_blockの日本株版）。

    ベンチマーク無し（大将「戦わなくていい。元本からどれだけ増えたかだけでしい」）のため、
    diff系の指標は全て元本(config.RSI_JP_INITIAL_CAPITAL_JPY)比で計算する。円建て・ドル換算はしない。
    """
    import jp_rsi_ledger

    principal_jpy = config.RSI_JP_INITIAL_CAPITAL_JPY
    diff_jpy = jp_nav_jpy - principal_jpy
    diff_pct = (diff_jpy / principal_jpy * 100.0) if principal_jpy else 0.0

    by_ticker: dict[str, dict[str, Any]] = {}
    for lot in jp_rsi_ledger.open_lots(jp_state):
        t = lot["ticker"]
        agg = by_ticker.setdefault(t, {"shares": 0.0, "invested": 0.0, "name": lot.get("name")})
        agg["shares"] += lot["shares"]
        agg["invested"] += lot["shares"] * lot["avg_cost"]

    holdings = []
    for ticker, agg in by_ticker.items():
        snap = jp_market_snap.get(ticker)
        if snap is None or agg["shares"] <= 0:
            continue
        value_jpy = agg["shares"] * snap.close
        weight_pct = (value_jpy / jp_nav_jpy * 100.0) if jp_nav_jpy else 0.0
        holdings.append({
            "ticker": ticker,
            "name": agg.get("name"),
            "shares": round(agg["shares"], 4),
            "value_jpy": round(value_jpy, 0),
            "weight_pct": round(weight_pct, 2),
            "price": round(snap.close, 2),
            "avg_cost": round(agg["invested"] / agg["shares"], 2),
            "link": _ticker_link(ticker, "JPY"),
        })
    holdings.sort(key=lambda h: -h["value_jpy"])

    history_rows = jp_rsi_ledger.read_history_rows()
    history = [
        {"date": r["date"], "nav": round(float(r["nav_jpy"]), 0), "principal": round(float(r["principal_jpy"]), 0)}
        for r in history_rows
    ]

    trade_rows = jp_rsi_ledger.read_trade_rows()
    trades_recent = trade_rows[-config.DATA_JSON_TRADES_LIMIT:]
    trades_recent.reverse()

    monthly = []
    by_month: dict[str, dict[str, Any]] = {}
    for row in history_rows:
        by_month[row["date"][:7]] = row
    for month, row in sorted(by_month.items()):
        nav = float(row["nav_jpy"])
        principal = float(row["principal_jpy"])
        monthly.append({
            "month": month,
            "nav": round(nav, 0),
            "principal": round(principal, 0),
            "result": "win" if nav >= principal else "lose",
        })

    return {
        "start_date": jp_state.get("start_date"),
        "nav_jpy": round(jp_nav_jpy, 0),
        "principal_jpy": round(principal_jpy, 0),
        "diff_jpy": round(diff_jpy, 0),
        "diff_pct": round(diff_pct, 2),
        "cash_jpy": round(jp_state.get("cash_jpy", 0.0), 0),
        "open_lots": len(jp_rsi_ledger.open_lots(jp_state)),
        "holdings": holdings,
        "history": history,
        "trades": trades_recent,
        "monthly": monthly,
    }


def build_data_json(
    state: dict[str, Any],
    market: dict[str, TickerSnapshot],
    nav_usd: float,
    bench_usd: float,
    accepted_trades: list[dict[str, Any]],
    now_jst: dt.datetime,
    rsi_block: dict[str, Any] | None = None,
    rsi_jp_block: dict[str, Any] | None = None,
) -> dict[str, Any]:
    diff_usd = nav_usd - bench_usd
    diff_pct = (diff_usd / bench_usd * 100.0) if bench_usd else 0.0
    principal_usd = config.INITIAL_CAPITAL_USD
    principal_diff_usd = nav_usd - principal_usd
    principal_diff_pct = (principal_diff_usd / principal_usd * 100.0) if principal_usd else 0.0

    from portfolio import compute_avg_costs, read_history_rows, read_trade_rows

    avg_costs = compute_avg_costs()
    theses = _load_theses()

    holdings = []
    for ticker, shares in state.get("holdings", {}).items():
        snap = market.get(ticker)
        if snap is None:
            continue
        currency = config.WHITELIST[ticker]["currency"]
        value_usd = shares * snap.close
        weight_pct = (value_usd / nav_usd * 100.0) if nav_usd else 0.0
        thesis = theses.get(ticker, {})
        holdings.append({
            "ticker": ticker,
            "name": config.WHITELIST[ticker]["name"],
            "shares": round(shares, 4),
            "value_usd": round(value_usd, 2),
            "weight_pct": round(weight_pct, 2),
            "price": round(snap.close, 4),
            "avg_cost": round(avg_costs.get(ticker, snap.close), 4),
            "currency": currency,
            "link": _ticker_link(ticker, currency),
            "reason": thesis.get("reason", ""),
            "target": thesis.get("target", ""),
        })

    history_rows = read_history_rows()
    history = [{"date": r["date"], "nav": round(float(r["nav_usd"]), 2), "bench": round(float(r["bench_usd"]), 2)}
               for r in history_rows]

    trade_rows = read_trade_rows()
    trades_recent = trade_rows[-config.DATA_JSON_TRADES_LIMIT:]
    trades_recent.reverse()
    # 本体のtrades.csvには銘柄名を持たせていない（config.WHITELISTに既にあるため。2026-08-26）。
    # 表示側でticker→nameを引くだけで済ませ、台帳スキーマは変更しない。
    for t in trades_recent:
        t["name"] = config.WHITELIST.get(t["ticker"], {}).get("name", "")

    monthly = _build_monthly_table(history_rows)

    return {
        "updated_at": now_jst.strftime("%Y-%m-%d %H:%M JST"),
        "start_date": state.get("start_date"),
        "nav_usd": round(nav_usd, 2),
        "bench_usd": round(bench_usd, 2),
        "diff_usd": round(diff_usd, 2),
        "diff_pct": round(diff_pct, 2),
        "principal_usd": round(principal_usd, 2),
        "principal_diff_usd": round(principal_diff_usd, 2),
        "principal_diff_pct": round(principal_diff_pct, 2),
        "mode": state.get("mode"),
        "holdings": holdings,
        "cash_usd": round(state.get("cash_usd", 0.0), 2),
        "history": history,
        "trades": trades_recent,
        "monthly": monthly,
        "rsi": rsi_block,
        "rsi_jp": rsi_jp_block,
    }


def _build_monthly_table(history_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """月末（各月の最終行）時点のnav/benchで月次勝敗をまとめる。"""
    by_month: dict[str, dict[str, Any]] = {}
    for row in history_rows:
        month = row["date"][:7]
        by_month[month] = row  # 日付昇順で読んでいるため最後の代入が月末値になる
    monthly = []
    for month, row in sorted(by_month.items()):
        nav = float(row["nav_usd"])
        bench = float(row["bench_usd"])
        monthly.append({
            "month": month,
            "nav": round(nav, 2),
            "bench": round(bench, 2),
            "result": "win" if nav >= bench else "lose",
        })
    return monthly


def save_data_json(data: dict[str, Any]) -> None:
    with open(config.DATA_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _trade_line(t: dict[str, Any], amount_key: str, symbol: str, decimals: int) -> str:
    """1件の取引をTelegram表示用の1行にする（会社名が取れていれば銘柄コードの後に添える。
    2026-08-26追加: 銘柄コードだけでは何を売買したか分からないという大将の指摘への対応）。
    """
    name = t.get("name")
    name_part = f" {name}" if name else ""
    amount = t[amount_key]
    return f"・{t['action']} {t['ticker']}{name_part} {symbol}{amount:,.{decimals}f}（{t['rule']}）"


def build_telegram_message(
    data: dict[str, Any],
    accepted_trades: list[dict[str, Any]],
    now_jst: dt.datetime,
    prev_month_result_line: str | None = None,
    alert_lines: list[str] | None = None,
    resolved_pending_lines: list[str] | None = None,
    rsi_accepted_trades: list[dict[str, Any]] | None = None,
    rsi_jp_accepted_trades: list[dict[str, Any]] | None = None,
) -> str:
    date_str = f"{now_jst.month}/{now_jst.day}"
    diff_usd = data["diff_usd"]
    diff_pct = data["diff_pct"]
    emoji = "🟢" if diff_usd >= 0 else "🔴"
    sign = "+" if diff_usd >= 0 else ""

    if accepted_trades:
        # 本体のtrades.csvには銘柄名を持たせていない（config.WHITELISTに既にあるため）ので、ここで引く
        enriched = [{**t, "name": config.WHITELIST.get(t["ticker"], {}).get("name", "")} for t in accepted_trades]
        trade_lines = "\n".join(_trade_line(t, "amount_usd", "$", 2) for t in enriched)
        trade_block = f"売買:\n{trade_lines}"
    else:
        trade_block = "売買: なし（ホールド）"

    lines: list[str] = []
    if alert_lines:
        lines.extend(alert_lines)
    lines.append(f"📊 vs S&P500 ({date_str})")
    if prev_month_result_line:
        lines.append(prev_month_result_line)
    lines.append(f"評価額: ${data['nav_usd']:,.2f}")
    lines.append(f"対S&P: {sign}${diff_usd:,.2f} ({sign}{diff_pct:.2f}%) {emoji}")
    lines.append(trade_block)

    if rsi_accepted_trades:
        rsi_trade_lines = "\n".join(_trade_line(t, "amount_usd", "$", 2) for t in rsi_accepted_trades)
        lines.append(f"RSI-30枠 売買:\n{rsi_trade_lines}")

    rsi_jp = data.get("rsi_jp")
    if rsi_jp and rsi_jp.get("start_date"):
        jp_diff = rsi_jp["diff_jpy"]
        jp_emoji = "🟢" if jp_diff >= 0 else "🔴"
        jp_sign = "+" if jp_diff >= 0 else ""
        lines.append(
            f"🇯🇵 日本株RSI枠: ¥{rsi_jp['nav_jpy']:,.0f}（元本比 {jp_sign}¥{jp_diff:,.0f} "
            f"{jp_sign}{rsi_jp['diff_pct']:.2f}%）{jp_emoji}"
        )

    if rsi_jp_accepted_trades:
        jp_trade_lines = "\n".join(_trade_line(t, "amount_jpy", "¥", 0) for t in rsi_jp_accepted_trades)
        lines.append(f"日本株RSI枠 売買:\n{jp_trade_lines}")

    if resolved_pending_lines:
        # 滞留・行方不明の注文を自己解決した記録（行動を求める警告ではなくFYI。2026-08-18 修正2）
        lines.append("処理ログ:\n" + "\n".join(f"・{m}" for m in resolved_pending_lines))
    lines.append(f"📈 {config.GITHUB_PAGES_URL}")
    return "\n".join(lines)


def send_telegram_message(text: str) -> bool:
    """Telegramへ送信する。

    環境変数 VS_SP500_DEFER_TELEGRAM=1 の時は送信せず pending_report.txt に貯める。
    夜間(23:00・米国市場の場中)に実行し、報告だけ朝(07:00)に届けるための仕組み。
    貯めた分は send_report.py がまとめて流す。
    """
    if os.environ.get("VS_SP500_DEFER_TELEGRAM") == "1":
        try:
            with open(config.PENDING_REPORT_PATH, "a", encoding="utf-8") as f:
                f.write(text.rstrip() + "\n\n")
            logger.info("Telegram送信を保留し pending_report.txt に追記した")
            return True
        except OSError as e:
            logger.error("保留ファイルへの書き込みに失敗: %s", e)
            return False

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
