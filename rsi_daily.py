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
import threading
from typing import Any, Callable

import broker
import config
import rsi_ledger
import rsi_strategy
import universe
from market import TickerSnapshot

logger = logging.getLogger("vs-sp500.rsi_daily")

_MOOMOO_CALL_TIMEOUT_SEC = 15.0  # broker.pyのCALL_TIMEOUT_SECに合わせる
_MOOMOO_SCREENER_PAGE_SIZE = 200  # moomoo 1リクエストの最大件数


def _run_with_timeout(fn: Callable[[], Any], timeout: float = _MOOMOO_CALL_TIMEOUT_SEC) -> Any | None:
    """broker.pyと同じ方式: デーモンスレッドで実行しtimeoutで見切りをつける。

    moomoo SDKが無応答で固まった実績がある（broker.py参照）ため、ここでも必ずタイムアウトを設ける。
    """
    result: dict[str, Any] = {}
    error: dict[str, Exception] = {}

    def _target() -> None:
        try:
            result["value"] = fn()
        except Exception as e:  # noqa: BLE001 - moomoo SDK内部の例外型は不定
            error["value"] = e

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    if thread.is_alive():
        logger.error("moomoo呼び出しがタイムアウトした（%s秒）", timeout)
        return None
    if "value" in error:
        logger.error("moomoo呼び出しが例外を送出した: %s", error["value"])
        return None
    return result.get("value")


def _ticker_from_code(code: str) -> str:
    """moomooのcode（例: 'US.AAPL'）からティッカーを取り出し、universe.pyと同じ正規化をする。"""
    raw = code.split(".", 1)[1] if "." in code else code
    return raw.strip().upper().replace(".", "-")


def _moomoo_snapshot(tickers: list[str]) -> dict[str, dict[str, Any]] | None:
    """get_market_snapshotで終値・日付を取得する。失敗時None。"""
    if not tickers:
        return {}

    def _call() -> dict[str, dict[str, Any]]:
        from moomoo import OpenQuoteContext

        ctx = OpenQuoteContext(host=config.MOOMOO_HOST, port=config.MOOMOO_PORT)
        try:
            codes = [f"US.{t}" for t in tickers]
            ret, data = ctx.get_market_snapshot(codes)
            if ret != 0:
                raise RuntimeError(f"get_market_snapshot失敗: {data}")
            result: dict[str, dict[str, Any]] = {}
            for row in data.to_dict(orient="records"):
                ticker = _ticker_from_code(str(row["code"]))
                update_time = str(row.get("update_time") or "")
                date = update_time.split(" ")[0] if update_time else dt.date.today().isoformat()
                result[ticker] = {"close": float(row["last_price"]), "date": date}
            return result
        finally:
            ctx.close()

    return _run_with_timeout(_call)


def fetch_market_data(tickers: list[str]) -> dict[str, dict[str, Any]]:
    """指定銘柄の直近終値・日付をmoomooから一括取得する（配当は常に0.0。個別株の配当は無視する方針のため）。

    個別銘柄の取得失敗は無視して続行する（1銘柄の欠測で全体が止まらないようにするため）。
    """
    if not tickers:
        return {}
    snapshot = _moomoo_snapshot(tickers)
    if snapshot is None:
        logger.error("RSI銘柄の価格取得(get_market_snapshot)に失敗した")
        return {}
    return {
        ticker: {"close": info["close"], "date": info["date"], "dividend": 0.0}
        for ticker, info in snapshot.items()
    }


def screen_rsi_candidates() -> list[dict[str, Any]]:
    """moomooスクリーナーでRSI(14) < 閾値 かつ 時価総額条件を満たす銘柄を抽出する（RSI昇順）。

    get_stock_filterを呼ぶ（時価総額の足切りにより通常は1ページで完結する）。
    last_pageがFalseの場合は警告ログを出したうえでページを繰り、取りこぼしを黙って発生させない。
    結果をuniverse.get_universe()の銘柄と突き合わせ、ユニバース内のものだけ残す。
    価格はget_market_snapshotで別途取得する。moomoo接続失敗時は空リストを返す。
    """
    uni_tickers = set(universe.get_universe())

    def _call() -> list[Any]:
        from moomoo import (
            CustomIndicatorFilter,
            KLType,
            Market,
            OpenQuoteContext,
            RelativePosition,
            SimpleFilter,
            StockField,
        )

        ctx = OpenQuoteContext(host=config.MOOMOO_HOST, port=config.MOOMOO_PORT)
        try:
            rsi_filter = CustomIndicatorFilter()
            rsi_filter.ktype = KLType.K_DAY
            rsi_filter.stock_field1 = StockField.RSI
            rsi_filter.stock_field1_para = [14]
            rsi_filter.stock_field2 = StockField.VALUE
            rsi_filter.value = config.RSI_ENTRY_RSI_THRESHOLD
            rsi_filter.relative_position = RelativePosition.LESS
            rsi_filter.is_no_filter = False

            cap_filter = SimpleFilter()
            cap_filter.stock_field = StockField.MARKET_VAL
            cap_filter.filter_min = config.RSI_SCREENER_MIN_MARKET_CAP_USD
            cap_filter.is_no_filter = False

            rows: list[Any] = []
            begin = 0
            while True:
                ret, ret_data = ctx.get_stock_filter(
                    market=Market.US, filter_list=[rsi_filter, cap_filter],
                    begin=begin, num=_MOOMOO_SCREENER_PAGE_SIZE,
                )
                if ret != 0:
                    raise RuntimeError(f"get_stock_filter失敗: {ret_data}")
                last_page, all_count, ret_list = ret_data
                rows.extend(ret_list)
                if last_page or not ret_list:
                    break
                logger.warning(
                    "RSIスクリーナー: last_pageがFalseのためページを繰る（取得済み%d件 / 全%d件）",
                    len(rows), all_count,
                )
                begin += len(ret_list)
            return rows
        finally:
            ctx.close()

    rows = _run_with_timeout(_call)
    if rows is None:
        logger.error("RSIスクリーナー(get_stock_filter)の呼び出しに失敗した")
        return []

    screened: list[dict[str, Any]] = []
    for row in rows:
        ticker = _ticker_from_code(str(row.stock_code))
        if ticker not in uni_tickers:
            continue
        rsi_val = row.__dict__.get(("rsi", "14", "k_day"))
        if rsi_val is None:
            continue
        screened.append({"ticker": ticker, "rsi14": float(rsi_val)})

    if not screened:
        return []

    prices = _moomoo_snapshot([c["ticker"] for c in screened])
    if prices is None:
        logger.error("RSI候補の価格取得(get_market_snapshot)に失敗した")
        return []

    candidates = [
        {"ticker": c["ticker"], "rsi14": c["rsi14"], "price": prices[c["ticker"]]["close"]}
        for c in screened
        if c["ticker"] in prices
    ]
    candidates.sort(key=lambda c: c["rsi14"])
    return candidates


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

    held_tickers = sorted({lot["ticker"] for lot in rsi_ledger.open_lots(state)})
    rsi_candidates = screen_rsi_candidates()
    candidate_tickers = [c["ticker"] for c in rsi_candidates]
    fetch_tickers = sorted(set(held_tickers) | set(candidate_tickers))
    market = fetch_market_data(fetch_tickers)
    log_lines.append(f"[RSI-1] 候補{len(rsi_candidates)}銘柄・保有{len(held_tickers)}銘柄・価格取得{len(market)}銘柄")

    do_trade = can_trade and not already_processed_today and not dry_run

    if do_trade:
        # --- 配当（個別株の配当は無視。ベンチマークのみVOO口数に再投資） ---
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
            {"ticker": c["ticker"], "rsi14": c["rsi14"], "price": market[c["ticker"]]["close"]}
            for c in rsi_candidates
            if c["ticker"] in market
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
