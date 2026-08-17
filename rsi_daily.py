"""vs-sp500: RSI-30枠の毎日実行ロジック（SPEC_RSI30.md準拠）。

daily_run.py が本体（配分戦略）の処理を終えた後に run() を1回呼ぶだけで完結する。
本体の台帳・判断・moomoo呼び出しには一切触れない（broker.pyはそのまま共用する。
place_market_orderは常にTrdEnv.SIMULATE・config.MOOMOO_ACC_IDに固定されているため、
このファイルが増えても実口座への発注リスクは増えない）。

処理順序（1日): 損切り→利確（SELL群を先に処理して現金を作る）→買い増し→新規エントリー
（買い増しはエントリーより優先。エントリーはRSIが低い順。SPEC_RSI30.md「資金不足時」準拠）。
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import yfinance as yf

import broker
import config
import rsi_ledger
import rsi_strategy
import universe
from market import TickerSnapshot, compute_rsi14

logger = logging.getLogger("vs-sp500.rsi_daily")

UNIVERSE_FETCH_PERIOD = "3mo"


def fetch_market_data(tickers: list[str]) -> dict[str, dict[str, Any]]:
    """指定銘柄の直近終値・日付・当日配当・RSI14を一括取得する。

    個別銘柄の取得失敗（上場廃止・ティッカー変更等）は無視して続行する
    （ユニバース全体が1銘柄の欠測で止まらないようにするため）。
    """
    if not tickers:
        return {}
    try:
        data = yf.download(
            tickers, period=UNIVERSE_FETCH_PERIOD, group_by="ticker",
            threads=True, auto_adjust=False, actions=True, progress=False,
        )
    except Exception as e:  # noqa: BLE001 - yfinance内部の例外型は不定
        logger.error("RSIユニバースの一括取得に失敗した: %s", e)
        return {}

    result: dict[str, dict[str, Any]] = {}
    single = len(tickers) == 1
    for ticker in tickers:
        try:
            df = data if single else data[ticker]
            closes = df["Close"].dropna()
            if closes.empty:
                continue
            last_close = float(closes.iloc[-1])
            last_date = closes.index[-1].date().isoformat()
            rsi14 = compute_rsi14(closes)
            dividend = 0.0
            if "Dividends" in df.columns:
                div_val = df["Dividends"].reindex(closes.index).iloc[-1]
                if div_val and not (isinstance(div_val, float) and div_val != div_val):  # NaN対策
                    dividend = float(div_val)
            result[ticker] = {"close": last_close, "date": last_date, "rsi14": rsi14, "dividend": dividend}
        except (KeyError, IndexError, ValueError, TypeError):
            continue
    return result


def _new_lot_id(ticker: str, entry_date: str, existing_lots: list[dict[str, Any]]) -> str:
    seq = sum(1 for lot in existing_lots if lot["ticker"] == ticker) + 1
    return f"{ticker}-{entry_date}-{seq}"


def _execute_order(ticker: str, qty: int, side: str) -> tuple[dict[str, Any] | None, float]:
    """broker発注し、実約定と現金差分(cash_delta)を返す。失敗時は(None, 0.0)。"""
    cash_before = broker.get_cash()
    fill = broker.place_market_order(ticker, qty, side)
    if fill is None:
        return None, 0.0
    cash_after = broker.get_cash()
    if cash_before is not None and cash_after is not None:
        cash_delta = cash_after - cash_before
    else:
        logger.warning("%s %s: moomoo現金取得に失敗したため約定額から見積もった", side, ticker)
        signed = -1 if side == "BUY" else 1
        cash_delta = signed * fill["filled_qty"] * fill["avg_price"]
    return fill, cash_delta


def compute_snapshot_only(
    rsi_state: dict[str, Any], voo_snap: TickerSnapshot,
) -> tuple[float, float, dict[str, TickerSnapshot]]:
    """保有銘柄の価格だけを取得してNAV/ベンチマークを計算する（ユニバース走査・売買は行わない）。

    --report-only・異常停止時など、表示更新のみが必要な場面で使う。
    """
    held_tickers = sorted({lot["ticker"] for lot in rsi_ledger.open_lots(rsi_state)})
    market = fetch_market_data(held_tickers) if held_tickers else {}
    market_snapshots = {
        t: TickerSnapshot(ticker=t, close=info["close"], date=info["date"], dividend=info.get("dividend", 0.0))
        for t, info in market.items()
    }
    nav_usd = rsi_ledger.compute_nav_usd(rsi_state, market_snapshots)
    bench_usd = rsi_ledger.compute_bench_nav_usd(rsi_state, voo_snap.close) if rsi_state.get("bench_units_rsi") else 0.0
    return nav_usd, bench_usd, market_snapshots


def run(
    rsi_state: dict[str, Any],
    voo_snap: TickerSnapshot,
    can_trade: bool,
    already_processed_today: bool,
    dry_run: bool,
    trade_date: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str], float, float, dict[str, TickerSnapshot]]:
    """RSI-30枠の1日分の処理を行う。

    戻り値: (更新後のrsi_state, 約定した取引ログ, ログ用メッセージ行, NAV, ベンチマーク評価額, 保有銘柄の市場スナップショット)
    """
    log_lines: list[str] = []
    state = dict(rsi_state)
    state["lots"] = [dict(lot) for lot in rsi_state.get("lots", [])]
    accepted_trades: list[dict[str, Any]] = []

    # 初回構築（本体のstart_date/bench_units構築と同じ扱い。何度呼ばれても1回しか発火しない）
    if state["start_date"] is None:
        state["start_date"] = voo_snap.date
        state["bench_units_rsi"] = config.RSI_INITIAL_CAPITAL_USD / voo_snap.close
        log_lines.append(f"[RSI-0] 初回構築: start_date={state['start_date']} bench_units_rsi={state['bench_units_rsi']:.6f}")

    uni_tickers = universe.get_universe()
    held_tickers = sorted({lot["ticker"] for lot in rsi_ledger.open_lots(state)})
    fetch_tickers = sorted(set(uni_tickers) | set(held_tickers))
    market = fetch_market_data(fetch_tickers)
    log_lines.append(f"[RSI-1] ユニバース{len(uni_tickers)}銘柄・保有{len(held_tickers)}銘柄・価格取得{len(market)}銘柄")

    do_trade = can_trade and not already_processed_today and not dry_run

    if do_trade:
        # --- 配当（保有銘柄の当日配当を現金に加算。ベンチマークはVOO口数に再投資） ---
        for lot in state["lots"]:
            if lot.get("closed"):
                continue
            info = market.get(lot["ticker"])
            if info and info.get("dividend", 0.0) > 0:
                state["cash_usd"] += lot["shares"] * info["dividend"]
        if voo_snap.dividend > 0 and state["bench_units_rsi"] > 0:
            state["bench_units_rsi"] += state["bench_units_rsi"] * voo_snap.dividend / voo_snap.close

        # --- 1. 損切り・利確（SELL群を先に処理して現金を作る） ---
        for lot in sorted(state["lots"], key=lambda x: (x["ticker"], x["lot_id"])):
            if lot.get("closed"):
                continue
            info = market.get(lot["ticker"])
            if info is None:
                logger.warning("RSI: %s の価格が取得できずロット%sの判定をスキップした", lot["ticker"], lot["lot_id"])
                continue
            price = info["close"]
            idx = next(i for i, x in enumerate(state["lots"]) if x["lot_id"] == lot["lot_id"])

            stop = rsi_strategy.decide_stop_loss(state["lots"][idx], price)
            if stop is not None:
                fill, cash_delta = _execute_order(stop["ticker"], stop["qty"], "SELL")
                if fill is None:
                    logger.warning("RSI損切り発注失敗: %s lot=%s", stop["ticker"], stop["lot_id"])
                    continue
                state["cash_usd"] += cash_delta
                state["lots"][idx] = rsi_strategy.apply_stop_loss_fill(
                    state["lots"][idx], fill["filled_qty"], trade_date,
                )
                trade_row = {
                    "date": trade_date, "action": "SELL", "ticker": stop["ticker"],
                    "shares": fill["filled_qty"], "price": round(fill["avg_price"], 4),
                    "amount_usd": round(fill["filled_qty"] * fill["avg_price"], 2),
                    "rule": "stop_loss", "lot_id": stop["lot_id"], "note": "",
                }
                rsi_ledger.append_trade_row(trade_row)
                accepted_trades.append(trade_row)
                continue  # 損切りした日は利確判定を行わない

            trading_days_elapsed = rsi_strategy.business_days_since(lot["initial_entry_date"], trade_date)
            while True:
                intents = rsi_strategy.decide_profit_takes(state["lots"][idx], price, trade_date, trading_days_elapsed)
                if not intents:
                    break
                intent = intents[0]
                if intent["kind"] == "exception_trigger":
                    state["lots"][idx] = rsi_strategy.apply_exception_trigger(state["lots"][idx])
                    break
                fill, cash_delta = _execute_order(intent["ticker"], intent["qty"], "SELL")
                if fill is None:
                    logger.warning("RSI利確発注失敗: %s lot=%s kind=%s", intent["ticker"], intent["lot_id"], intent["kind"])
                    break
                state["cash_usd"] += cash_delta
                if intent["kind"] == "profit1":
                    state["lots"][idx] = rsi_strategy.apply_profit1_fill(
                        state["lots"][idx], fill["filled_qty"], intent["base_shares"],
                    )
                else:
                    state["lots"][idx] = rsi_strategy.apply_profit2_fill(state["lots"][idx], fill["filled_qty"])
                trade_row = {
                    "date": trade_date, "action": "SELL", "ticker": intent["ticker"],
                    "shares": fill["filled_qty"], "price": round(fill["avg_price"], 4),
                    "amount_usd": round(fill["filled_qty"] * fill["avg_price"], 2),
                    "rule": intent["kind"], "lot_id": intent["lot_id"], "note": "",
                }
                rsi_ledger.append_trade_row(trade_row)
                accepted_trades.append(trade_row)

        # --- 2. 買い増し（新規エントリーより優先） ---
        for lot in sorted(state["lots"], key=lambda x: (x["ticker"], x["lot_id"])):
            if lot.get("closed"):
                continue
            info = market.get(lot["ticker"])
            if info is None:
                continue
            price = info["close"]
            idx = next(i for i, x in enumerate(state["lots"]) if x["lot_id"] == lot["lot_id"])
            for intent in rsi_strategy.decide_pyramid_buys(state["lots"][idx], price):
                qty = int(intent["amount_usd"] / price + 1e-9)
                if qty <= 0:
                    continue
                estimated_cost = qty * price
                if estimated_cost > state["cash_usd"] + 1e-6:
                    logger.info("RSI買い増しスキップ（現金不足）: %s %s", intent["ticker"], intent["kind"])
                    continue
                fill, cash_delta = _execute_order(intent["ticker"], qty, "BUY")
                if fill is None:
                    logger.warning("RSI買い増し発注失敗: %s lot=%s kind=%s", intent["ticker"], intent["lot_id"], intent["kind"])
                    continue
                state["cash_usd"] += cash_delta
                state["lots"][idx] = rsi_strategy.apply_pyramid_fill(
                    state["lots"][idx], intent["stage_index"], fill["filled_qty"], fill["avg_price"],
                )
                trade_row = {
                    "date": trade_date, "action": "BUY", "ticker": intent["ticker"],
                    "shares": fill["filled_qty"], "price": round(fill["avg_price"], 4),
                    "amount_usd": round(fill["filled_qty"] * fill["avg_price"], 2),
                    "rule": intent["kind"], "lot_id": intent["lot_id"], "note": "",
                }
                rsi_ledger.append_trade_row(trade_row)
                accepted_trades.append(trade_row)

        # --- 3. 新規エントリー（RSIが低い順。現金が足りる分だけ） ---
        candidates = [
            {"ticker": t, "rsi14": info["rsi14"], "price": info["close"]}
            for t, info in market.items()
            if t in uni_tickers and rsi_strategy.should_enter(info["rsi14"])
        ]
        selected = rsi_strategy.select_entries_within_cash(candidates, state["cash_usd"])
        for cand in selected:
            fill, cash_delta = _execute_order(cand["ticker"], cand["qty"], "BUY")
            if fill is None:
                logger.warning("RSI新規エントリー発注失敗: %s", cand["ticker"])
                continue
            state["cash_usd"] += cash_delta
            lot_id = _new_lot_id(cand["ticker"], trade_date, state["lots"])
            new_lot = rsi_strategy.new_lot(cand["ticker"], lot_id, trade_date, fill["filled_qty"], fill["avg_price"])
            state["lots"].append(new_lot)
            trade_row = {
                "date": trade_date, "action": "BUY", "ticker": cand["ticker"],
                "shares": fill["filled_qty"], "price": round(fill["avg_price"], 4),
                "amount_usd": round(fill["filled_qty"] * fill["avg_price"], 2),
                "rule": "entry", "lot_id": lot_id, "note": f"RSI14={cand['rsi14']:.1f}",
            }
            rsi_ledger.append_trade_row(trade_row)
            accepted_trades.append(trade_row)

        state["last_processed_date"] = trade_date
        log_lines.append(f"[RSI-2] 約定{len(accepted_trades)}件（損切り/利確/買い増し/新規エントリー込み）")
    else:
        reason = "dry-run" if dry_run else ("休場/処理済み" if already_processed_today else "売買停止中")
        log_lines.append(f"[RSI-2] 売買スキップ（{reason}）")

    market_snapshots = {
        t: TickerSnapshot(ticker=t, close=info["close"], date=info["date"], dividend=info.get("dividend", 0.0))
        for t, info in market.items()
    }
    nav_usd = rsi_ledger.compute_nav_usd(state, market_snapshots)
    bench_usd = rsi_ledger.compute_bench_nav_usd(state, voo_snap.close)
    diff_usd = nav_usd - bench_usd
    cash_ratio = rsi_ledger.compute_cash_ratio(state, nav_usd) if nav_usd else 0.0
    log_lines.append(f"[RSI-3] 評価額: NAV=${nav_usd:,.2f} ベンチマーク=${bench_usd:,.2f} 差額=${diff_usd:,.2f}")

    if not dry_run:
        rsi_ledger.append_history_row({
            "date": voo_snap.date,
            "nav_usd": round(nav_usd, 2),
            "bench_usd": round(bench_usd, 2),
            "diff_usd": round(diff_usd, 2),
            "diff_pct": round(diff_usd / bench_usd * 100, 4) if bench_usd else 0.0,
            "cash_ratio": round(cash_ratio, 4),
            "open_lots": len(rsi_ledger.open_lots(state)),
        })
        rsi_ledger.save_portfolio(state)
        log_lines.append("[RSI-4] 台帳保存完了")
    else:
        log_lines.append("[RSI-4] dry-runのため台帳保存はスキップ")

    held_snapshots = {
        t: snap for t, snap in market_snapshots.items()
        if t in {lot["ticker"] for lot in rsi_ledger.open_lots(state)}
    }
    return state, accepted_trades, log_lines, nav_usd, bench_usd, held_snapshots
