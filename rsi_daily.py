"""vs-sp500: RSI-30枠の毎日実行ロジック（SPEC_RSI30.md準拠）。

daily_run.py が本体（配分戦略）の処理を終えた後に run() を1回呼ぶだけで完結する。
本体の台帳・判断・moomoo呼び出しには一切触れない（broker.pyはそのまま共用する。
place_market_orderは常にTrdEnv.SIMULATE・config.MOOMOO_ACC_IDに固定されているため、
このファイルが増えても実口座への発注リスクは増えない）。

処理順序（1日): 損切り→利確（SELL群を先に処理して現金を作る）→買い増し→新規エントリー
（買い増しはエントリーより優先。エントリーはRSIが低い順。SPEC_RSI30.md「資金不足時」準拠）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import threading
import time
from typing import Any, Callable

import broker
import config
import dividends
import jp_rsi_daily
import rsi_ledger
import rsi_strategy
import sector_map
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


def _moomoo_snapshot(tickers: list[str]) -> dict[str, dict[str, Any]] | None:
    """get_market_snapshotで終値・日付を取得する。失敗時None。"""
    if not tickers:
        return {}

    def _call() -> dict[str, dict[str, Any]]:
        from moomoo import OpenQuoteContext

        ctx = OpenQuoteContext(host=config.MOOMOO_HOST, port=config.MOOMOO_PORT)
        try:
            codes = [broker.ticker_to_code(t) for t in tickers]
            ret, data = ctx.get_market_snapshot(codes)
            if ret != 0:
                raise RuntimeError(f"get_market_snapshot失敗: {data}")
            result: dict[str, dict[str, Any]] = {}
            for row in data.to_dict(orient="records"):
                ticker = broker.code_to_ticker(str(row["code"]))
                update_time = str(row.get("update_time") or "")
                date = update_time.split(" ")[0] if update_time else dt.date.today().isoformat()
                result[ticker] = {"close": float(row["last_price"]), "date": date}
            return result
        finally:
            ctx.close()

    return _run_with_timeout(_call)


def fetch_market_data(tickers: list[str]) -> dict[str, dict[str, Any]]:
    """指定銘柄の直近終値・日付をmoomooから一括取得する。

    個別銘柄の取得失敗は無視して続行する（1銘柄の欠測で全体が止まらないようにするため）。
    """
    if not tickers:
        return {}
    snapshot = _moomoo_snapshot(tickers)
    if snapshot is None:
        logger.error("RSI銘柄の価格取得(get_market_snapshot)に失敗した")
        return {}
    return snapshot


def screen_rsi_candidates() -> list[dict[str, Any]] | None:
    """moomooスクリーナーでRSI(14) < 閾値 かつ 時価総額条件を満たす銘柄を抽出する（RSI昇順）。

    get_stock_filterを呼ぶ（時価総額の足切りにより通常は1ページで完結する）。
    last_pageがFalseの場合は警告ログを出したうえでページを繰り、取りこぼしを黙って発生させない。
    結果をuniverse.get_universe()の銘柄と突き合わせ、ユニバース内のものだけ残す。
    価格はget_market_snapshotで別途取得する。

    戻り値: moomoo接続・取得に失敗した場合はNone。取得に成功したが該当銘柄が0件の場合は
    空リスト（[]）を返す。呼び出し側（freeze_candidates）はこの2つを区別してリトライ要否を
    判断する（2026-08-19改修3。RSI32以下が本当に0件の日にNoneと空リストが同一視され、
    無駄なリトライ待機とfrozen_candidates.json未更新を招いていた対策）。
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
        return None

    screened: list[dict[str, Any]] = []
    for row in rows:
        ticker = broker.code_to_ticker(str(row.stock_code))
        if ticker not in uni_tickers:
            continue
        rsi_val = row.__dict__.get(("rsi", "14", "k_day"))
        if rsi_val is None:
            continue
        screened.append({"ticker": ticker, "rsi14": float(rsi_val), "name": row.stock_name})

    if not screened:
        return []

    prices = _moomoo_snapshot([c["ticker"] for c in screened])
    if prices is None:
        logger.error("RSI候補の価格取得(get_market_snapshot)に失敗した")
        return None

    candidates = [
        {
            "ticker": c["ticker"], "rsi14": c["rsi14"], "name": c.get("name"),
            "price": prices[c["ticker"]]["close"], "date": prices[c["ticker"]]["date"],
        }
        for c in screened
        if c["ticker"] in prices
    ]
    candidates.sort(key=lambda c: c["rsi14"])
    return candidates


def _sleep_seconds(seconds: float) -> None:
    """time.sleepの薄いラッパー（テストでリトライ待ちをモックできるようにするため）。"""
    time.sleep(seconds)


def freeze_candidates() -> dict[str, Any] | None:
    """RSI候補を寄り前(現地20:00launchdジョブ)に確定し frozen_candidates.json へ保存する。

    screen_rsi_candidates()を1回呼ぶだけ。発注はせず、台帳も一切変更しない
    （2026-08-19改修1-a）。市場が閉まっている時間に叩けば、返るRSIは前日終値ベースになる。
    どの基準で取得したかはbroker.is_market_open_us()の実測で判定してrsi_basisに記録する
    （常に"prev_close"と決め打ちしない。手動再実行等で場中に叩かれる場合もありうるため）。

    moomooから候補を取得できなかった場合（OpenD未接続・呼び出し失敗）は、
    間隔を空けて最大config.RSI_FREEZE_CANDIDATES_MAX_ATTEMPTS回まで再試行する
    （2026-08-19改修2。候補確定ジョブが失敗すると黙って場中の値に切り替わっていた対策）。
    全試行が失敗したらログに残してNoneを返し、ファイルは書かない
    （古い候補を残したまま次回に賭ける。Telegram通知はしない＝通知ではなく自動復旧の方針）。

    取得自体に成功したが該当銘柄が0件だった場合はリトライしない。RSI32以下の銘柄が
    本当に1つも無い日（相場が強い日）は正常にありうるため、空の候補で即座に確定させる
    （2026-08-19改修3。以前は0件をNoneと区別できず無駄にリトライ待機していた）。
    """
    max_attempts = config.RSI_FREEZE_CANDIDATES_MAX_ATTEMPTS
    candidates: list[dict[str, Any]] | None = None
    for attempt in range(1, max_attempts + 1):
        if not broker.is_available():
            logger.error("候補確定 試行%d/%d回目: moomoo未接続", attempt, max_attempts)
        else:
            candidates = screen_rsi_candidates()
            if candidates is not None:
                break  # 空リスト([])も取得成功として即座に受け入れる
            logger.error("候補確定 試行%d/%d回目: 候補の取得に失敗した", attempt, max_attempts)

        if attempt < max_attempts:
            delay = config.RSI_FREEZE_CANDIDATES_RETRY_DELAYS_SEC[attempt - 1]
            logger.warning("候補確定: %.0f秒待って再試行する", delay)
            _sleep_seconds(delay)

    if candidates is None:
        logger.error("候補確定: %d回試行しても取得できなかったため中止した", max_attempts)
        return None

    market_open = broker.is_market_open_us()
    # Noneで基準が確定できない場合は「前日終値ベース」と言い切れないためliveとして扱う
    rsi_basis = "prev_close" if market_open is False else "live"

    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "rsi_basis": rsi_basis,
        "candidates": candidates,
    }
    config.RSI_LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    config.RSI_FROZEN_CANDIDATES_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    logger.info(
        "候補確定完了: %d件 rsi_basis=%s → %s",
        len(candidates), rsi_basis, config.RSI_FROZEN_CANDIDATES_PATH,
    )
    return payload


def _load_frozen_candidates() -> dict[str, Any] | None:
    """frozen_candidates.jsonを読む。無い・壊れている・12時間より古ければNoneを返す。"""
    path = config.RSI_FROZEN_CANDIDATES_PATH
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        generated_at = dt.datetime.fromisoformat(payload["generated_at"])
    except (OSError, ValueError, KeyError) as e:
        logger.warning("frozen_candidates.jsonの読み込みに失敗した: %s", e)
        return None
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=dt.timezone.utc)
    age_hours = (dt.datetime.now(dt.timezone.utc) - generated_at).total_seconds() / 3600
    if age_hours > config.RSI_FROZEN_CANDIDATES_MAX_AGE_HOURS:
        logger.warning("frozen_candidates.jsonが%.1f時間前と古いため無視する", age_hours)
        return None
    return payload


def get_rsi_candidates() -> tuple[list[dict[str, Any]], str]:
    """執行時に使うRSI候補と、その基準(rsi_basis)を返す（2026-08-19改修1-b/1-c）。

    frozen_candidates.jsonが新しければそれをそのまま使う（執行時にRSIを再判定しない。
    RSIが閾値を超えていても大将の指示どおり買う）。無い・古い場合はその場でスクリーナーを
    叩いて売買を止めない。その場合は場中の値になるためrsi_basis="live"とする。
    """
    frozen = _load_frozen_candidates()
    if frozen is not None:
        return frozen["candidates"], frozen["rsi_basis"]
    logger.warning("frozen_candidates.jsonが無いか古いため、その場でRSIスクリーナーを実行する")
    candidates = screen_rsi_candidates()
    return candidates or [], "live"


def _new_lot_id(
    ticker: str, entry_date: str, existing_lots: list[dict[str, Any]],
    pending_orders: list[dict[str, Any]] | None = None,
) -> str:
    """新しいlot_idを発番する。

    未決のentry注文（まだロットが作られていない）も同じticker分を予約済みとして数える。
    そうしないと、entryが未約定のままpending_orders行きになった翌日以降に同一銘柄へ
    再エントリーした場合、後で決済されたときにlot_idが衝突しうる
    （SPEC「再エントリーにクールダウンは無い」により同一銘柄の複数ロットは正規に起こりうる）。
    """
    seq = sum(1 for lot in existing_lots if lot["ticker"] == ticker)
    seq += sum(1 for o in (pending_orders or []) if o.get("ticker") == ticker and o.get("rule") == "entry")
    return f"{ticker}-{entry_date}-{seq + 1}"


def _execute_order(
    ticker: str, qty: int, side: str, market_us: str | None,
) -> tuple[dict[str, Any] | None, float]:
    """broker発注し、実約定と現金差分(cash_delta)を返す。失敗時は(None, 0.0)。

    market_us: run()が1回の実行につき1回だけ問い合わせて渡す市場状態（2026-08-18 修正2）。
    AFTERNOON以外（かつNoneでない）なら発注せず見送る。問い合わせ失敗時（None）はフェイルオープン。
    """
    if market_us is not None and market_us != broker.MARKET_US_OPEN_STATE:
        logger.info(
            "RSI %s %s: 市場が開いていないため発注を見送った（market_us=%s）", side, ticker, market_us,
        )
        return None, 0.0
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


def settle_pending_orders(
    rsi_state: dict[str, Any], today: str, dry_run: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str], list[str]]:
    """RSI枠のpending_ordersを毎回の実行冒頭でmoomooに問い合わせ、確定した分をロット・現金へ反映する。

    本体のsettle_pending_orders（portfolio.py）と同じ決済方針だが、反映先がロットである点が異なる。
    entryロット（rule="entry"）はpending時点ではまだ存在しないため、ここで初めて生成する
    （SPEC「entryで建ったロットが後日約定した場合、ロットが正しく生成されること」）。
    本体のpending_ordersには一切触れない。

    問い合わせ成功・注文が見つからない（NOT_FOUND）場合、および市場が開いているのにまだ
    未約定（非終端ステータス）の場合の自己解決は本体と同じ方針（2026-08-18 修正2。
    portfolio.settle_pending_ordersのdocstring参照）。

    戻り値: (新しいstate, 反映した取引ログ, 警告メッセージのリスト, 自己解決の記録メッセージのリスト)
    """
    state = dict(rsi_state)
    state["lots"] = [dict(lot) for lot in rsi_state.get("lots", [])]
    pending = list(rsi_state.get("pending_orders", []))
    remaining: list[dict[str, Any]] = []
    applied_trades: list[dict[str, Any]] = []
    warnings: list[str] = []
    resolved_notes: list[str] = []
    market_open = broker.is_market_open_us() if pending else None

    for order in pending:
        info = broker.get_order_status(order["order_id"])
        if info is None:
            remaining.append(order)
            continue

        if info["status"] == "NOT_FOUND":
            resolved_notes.append(
                f"{order['ticker']}のRSI枠注文(order_id={order['order_id']})が見つからずpendingから除外した"
                f"（rule={order.get('rule')}）"
            )
            continue  # ロット・現金は無変更のままpending_ordersから外す

        applied_qty = order.get("applied_qty", 0)
        applied_value = order.get("applied_value_usd", 0.0)
        dealt_qty = info["filled_qty"]
        dealt_avg_price = info["avg_price"]
        status = info["status"]

        new_fill_qty = dealt_qty - applied_qty
        if new_fill_qty > 0:
            total_value = dealt_qty * dealt_avg_price
            incremental_value = total_value - applied_value
            incremental_price = incremental_value / new_fill_qty
            fill_date = info.get("updated_date") or order["submitted_date"]
            rule = order["rule"]
            ticker = order["ticker"]
            lot_id = order.get("lot_id", "")

            applied_ok = True
            lot_name = order.get("name")
            if rule == "entry":
                new_lot = rsi_strategy.new_lot(ticker, lot_id, fill_date, new_fill_qty, incremental_price, name=lot_name)
                state["lots"].append(new_lot)
            else:
                idx = next((i for i, x in enumerate(state["lots"]) if x["lot_id"] == lot_id), None)
                lot_avg_cost = state["lots"][idx]["avg_cost"] if idx is not None else None
                if idx is None:
                    warnings.append(
                        f"pending決済: lot_id={lot_id} が見つからない（{ticker}・{rule}）。反映をスキップした"
                    )
                    remaining.append(order)
                    applied_ok = False
                elif rule.startswith("pyramid"):
                    state["lots"][idx] = rsi_strategy.apply_pyramid_fill(
                        state["lots"][idx], order["stage_index"], new_fill_qty, incremental_price,
                    )
                    lot_name = state["lots"][idx].get("name")
                elif rule == "profit1":
                    state["lots"][idx] = rsi_strategy.apply_profit1_fill(
                        state["lots"][idx], new_fill_qty, order["base_shares"],
                    )
                    lot_name = state["lots"][idx].get("name")
                elif rule == "profit2":
                    state["lots"][idx] = rsi_strategy.apply_profit2_fill(state["lots"][idx], new_fill_qty)
                    lot_name = state["lots"][idx].get("name")
                elif rule == "stop_loss":
                    state["lots"][idx] = rsi_strategy.apply_stop_loss_fill(state["lots"][idx], new_fill_qty, fill_date)
                    lot_name = state["lots"][idx].get("name")
                else:
                    warnings.append(f"pending決済: 未知のrule={rule}（lot_id={lot_id}）。反映をスキップした")
                    remaining.append(order)
                    applied_ok = False

            if not applied_ok:
                continue  # ロットが見つからない等の異常。この項目はpending_ordersに残したまま次回再試行する

            side = order["side"]
            if side == "BUY":
                state["cash_usd"] -= incremental_price * new_fill_qty
            else:
                state["cash_usd"] += incremental_price * new_fill_qty

            realized_pnl, realized_pnl_pct = "", ""
            if side == "SELL" and lot_avg_cost:
                realized_pnl, realized_pnl_pct = rsi_strategy.compute_realized_pnl(
                    lot_avg_cost, incremental_price, new_fill_qty,
                )

            trade_row = {
                "date": fill_date, "action": side, "ticker": ticker,
                "shares": new_fill_qty, "price": round(incremental_price, 4),
                "amount_usd": round(new_fill_qty * incremental_price, 2),
                "rule": rule, "lot_id": lot_id,
                "realized_pnl": realized_pnl, "realized_pnl_pct": realized_pnl_pct,
                "note": f"pending決済(order_id={order['order_id']})・手数料不明のため未計上",
                "name": lot_name,
            }
            applied_trades.append(trade_row)
            warnings.append(
                f"pending決済: {ticker} {side} {new_fill_qty}株 @ {incremental_price:.4f}"
                f"（order_id={order['order_id']}・手数料は台帳に未計上）"
            )

            applied_qty = dealt_qty
            applied_value = total_value

        if status == "FILLED_ALL" or applied_qty >= order["qty"]:
            continue  # 全量確定。pending_ordersから外す

        if status in broker.ORDER_TERMINAL_STATUSES:
            if applied_qty < order["qty"]:
                warnings.append(
                    f"未決注文が未達のまま終端した（status={status}）: order_id={order['order_id']} "
                    f"{order['ticker']} {order.get('rule')} 残数{order['qty'] - applied_qty}株は打ち切り"
                )
            continue  # 終端。pending_ordersから外す

        # 修正2a: 市場が開いているのにまだ未約定 → 自動でキャンセルして解決する（本体と同じ方針）
        if market_open is True:
            if dry_run:
                warnings.append(
                    f"[dry-run] 滞留注文のキャンセル対象（実行はスキップ）: order_id={order['order_id']} "
                    f"{order['ticker']}"
                )
                remaining.append({**order, "applied_qty": applied_qty, "applied_value_usd": applied_value})
                continue
            if broker.cancel_order(order["order_id"]):
                resolved_notes.append(
                    f"{order['ticker']}のRSI枠注文(order_id={order['order_id']})が場中に滞留したため"
                    f"キャンセルした（適用済み{applied_qty}/{order['qty']}株・rule={order.get('rule')}。"
                    "以降は通常判断に委ねる）"
                )
            else:
                warnings.append(
                    f"滞留注文のキャンセル要求に失敗した: order_id={order['order_id']} {order['ticker']}"
                    "（次回も未決のまま再試行される）"
                )
                remaining.append({**order, "applied_qty": applied_qty, "applied_value_usd": applied_value})
            continue

        remaining.append({**order, "applied_qty": applied_qty, "applied_value_usd": applied_value})

    state["pending_orders"] = remaining
    return state, applied_trades, warnings, resolved_notes


def adjust_lots_for_splits(state: dict[str, Any], trade_date: str, log_lines: list[str]) -> None:
    """保有ロットに建て後の株式分割があれば、判定の前に株数・単価を分割後の値へ直す（2026-10-01追加）。

    大将「分割は分割できちんと計算しないとね。」。分割情報はmoomooのget_rehab（charter v1.6
    「基本データ…moomooにしなさいよ」）。取得に失敗したらWARNINGを出して調整せずに続行する
    （実行は止めない）。state["lots"]を置き換える。
    """
    held_tickers = sorted({lot["ticker"] for lot in rsi_ledger.open_lots(state)})
    if not held_tickers:
        return
    splits_by_ticker = broker.get_splits(held_tickers)
    if splits_by_ticker is None:
        logger.warning("RSI: moomooから分割情報が取得できず、分割調整なしで判定する")
        log_lines.append("[RSI-0] 警告: 分割情報の取得に失敗（分割調整なしで続行）")
        splits_by_ticker = {}
    for ticker in held_tickers:
        if ticker not in splits_by_ticker:
            logger.warning("RSI: %s の分割情報が取得できず、分割調整なしで判定する", ticker)
    new_lots = []
    for lot in state["lots"]:
        new_lot, applied = rsi_strategy.adjust_lot_for_splits(
            lot, splits_by_ticker.get(lot["ticker"], []), trade_date,
        )
        if applied:
            splits_text = ", ".join(f"{d} 1:{r:g}" for d, r in applied)
            msg = (
                f"[RSI-0] 株式分割を反映: {lot['ticker']} ({splits_text}) "
                f"株数{lot['shares']}→{new_lot['shares']} 初期単価{lot['initial_entry_price']:.4f}→"
                f"{new_lot['initial_entry_price']:.4f}"
            )
            logger.warning(msg)
            log_lines.append(msg)
        new_lots.append(new_lot)
    state["lots"] = new_lots


def _split_adjusted_stop_loss_history(
    stop_loss_history: dict[str, dict[str, Any]], trade_date: str, log_lines: list[str],
) -> dict[str, dict[str, Any]]:
    """損切り後の再エントリー制限で使う売却価格Pを、売却日より後の株式分割があれば調整する
    （2026-10-07追加）。分割情報はmoomooのget_rehab（adjust_lots_for_splitsと同じ取得元）。
    取得に失敗した銘柄はWARNINGを出し、分割調整なし（元の価格のまま）で続行する（実行は止めない）。
    """
    if not stop_loss_history:
        return stop_loss_history
    tickers = sorted(stop_loss_history.keys())
    splits_by_ticker = broker.get_splits(tickers)
    if splits_by_ticker is None:
        logger.warning("RSI: moomooから分割情報が取得できず、損切り再エントリー判定は分割調整なしで続行する")
        log_lines.append("[RSI-1] 警告: 損切り再エントリー判定の分割情報取得に失敗（分割調整なしで続行）")
        splits_by_ticker = {}
    adjusted: dict[str, dict[str, Any]] = {}
    for ticker, sl in stop_loss_history.items():
        if ticker not in splits_by_ticker:
            logger.warning("RSI: %s の分割情報が取得できず、損切り再エントリー判定は分割調整なしで続行する", ticker)
            adjusted[ticker] = sl
            continue
        new_price = rsi_strategy.adjust_stop_loss_price_for_splits(
            sl["price"], sl["date"], splits_by_ticker[ticker], trade_date,
        )
        adjusted[ticker] = {**sl, "price": new_price}
    return adjusted


def credit_dividends(state: dict[str, Any], log_lines: list[str]) -> None:
    """保有中・過去保有ロットの配当をcash_usdへ記帳する（2026-10-07追加・Change3）。

    moomooのget_rehab（per_cash_div + special_dividend）から配当履歴を取得し、未記帳の
    (ticker, ex_date, lot_id)だけをledger/rsi/dividends.csvへ追記してcash_usdに加算する。
    呼び出し側（run_rsi_dayのdry_runガード）がdry-runでは呼ばない前提（ファイルI/Oを含むため）。
    取得失敗時はWARNINGを出して記帳なしで続行する（実行は止めない）。
    """
    held_tickers = sorted({lot["ticker"] for lot in rsi_ledger.open_lots(state)})
    if not held_tickers:
        return
    dividends_by_ticker = broker.get_dividends(held_tickers)
    if dividends_by_ticker is None:
        logger.warning("RSI: moomooから配当情報が取得できず、配当記帳なしで続行する")
        log_lines.append("[RSI-0] 警告: 配当情報の取得に失敗（配当記帳なしで続行）")
        return
    existing_keys = {(r["ticker"], r["date"], r["lot_id"]) for r in rsi_ledger.read_dividend_rows()}
    trades = rsi_ledger.read_trade_rows()
    new_rows = dividends.compute_new_dividend_credits(
        state["lots"], trades, dividends_by_ticker, existing_keys, source="moomoo",
    )
    for row in new_rows:
        rsi_ledger.append_dividend_row(row)
        state["cash_usd"] += row["amount"]
        log_lines.append(
            f"[RSI-DIV] {row['ticker']} lot={row['lot_id']} {row['date']} "
            f"{row['shares']}株×${row['per_share']:.4f} = ${row['amount']:.2f}"
        )


def _month_key(date_str: str) -> str:
    return date_str[:7]  # "YYYY-MM"


def get_sector_map(
    universe_tickers: list[str], trade_date: str, dry_run: bool, log_lines: list[str],
) -> dict[str, str | None]:
    """ticker→SPDRセクターETF（不明はNone）のマッピングをledger/rsi/sector_map.jsonにキャッシュする
    （2026-10-07追加・スワップ売却機能）。universe.jsonと同じ月次周期（当月分のキャッシュが
    無ければ更新。「資金不足の候補がある夜」にだけ呼ばれるため、universe.json更新と厳密に
    同じ日とは限らない＝「その月で初めてスワップ判定が必要になった日」に更新される）。

    dry_run=Trueの場合、更新が必要でもファイルへは書かず（dry-runはledger/配下を変更しない
    契約を守るため）、その場で取得した値をそのまま返す（プレビュー用。次回の本番実行時に
    改めて取得・保存される）。moomoo取得に失敗した場合は既存キャッシュ（無ければ全銘柄unknown）
    で続行する（実行は止めない）。
    """
    path = config.RSI_SWAP_SECTOR_MAP_PATH
    month = _month_key(trade_date)
    cache: dict[str, Any] | None = None
    if path.exists():
        try:
            cache = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning("sector_map.json読み込み失敗: %s", e)
            cache = None
    if cache is not None and cache.get("month") == month:
        return cache["map"]

    plates = broker.get_owner_plates(universe_tickers)
    if plates is None:
        msg = "[RSI-SWAP] 警告: sector_map更新に失敗（get_owner_plate）。既存キャッシュ(無ければ全銘柄unknown)で続行"
        logger.warning(msg)
        log_lines.append(msg)
        return (cache or {}).get("map", {t: None for t in universe_tickers})

    new_map = {t: sector_map.classify_industry_labels(plates.get(t, [])) for t in universe_tickers}
    if dry_run:
        log_lines.append(f"[RSI-SWAP] [dry-run] sector_map更新対象だが保存はスキップ（{len(universe_tickers)}銘柄・{month}）")
        return new_map

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"month": month, "map": new_map}, ensure_ascii=False, indent=2), encoding="utf-8")
    log_lines.append(f"[RSI-SWAP] sector_map更新完了: {len(universe_tickers)}銘柄 ({month})")
    return new_map


def get_sector_tiers(trade_date: str, dry_run: bool, log_lines: list[str]) -> dict[str, int]:
    """SPDRセクターETF11本の直近リターンから月次のセクターTier(-1/0/+1)を
    ledger/rsi/sector_tiers.jsonにキャッシュする（2026-10-07追加・スワップ売却機能）。

    dry_run=Trueの場合はget_sector_mapと同じ方針（更新が必要でもファイルへは書かずその場の値を返す）。
    """
    path = config.RSI_SWAP_SECTOR_TIERS_PATH
    month = _month_key(trade_date)
    cache: dict[str, Any] | None = None
    if path.exists():
        try:
            cache = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning("sector_tiers.json読み込み失敗: %s", e)
            cache = None
    if cache is not None and cache.get("month") == month:
        return cache["tiers"]

    returns = broker.get_sector_etf_returns(
        config.RSI_SWAP_SECTOR_ETFS, config.RSI_SWAP_SECTOR_RETURN_LOOKBACK_TRADING_DAYS,
    )
    if returns is None or len(returns) < len(config.RSI_SWAP_SECTOR_ETFS):
        msg = "[RSI-SWAP] 警告: セクターTier更新に失敗（ETFリターン取得不足）。既存キャッシュ(無ければ全セクター0)で続行"
        logger.warning(msg)
        log_lines.append(msg)
        return (cache or {}).get("tiers", {})

    tiers = rsi_strategy.rank_sector_etf_tiers(returns)
    if dry_run:
        log_lines.append(f"[RSI-SWAP] [dry-run] セクターTier更新対象だが保存はスキップ（{month}）: {tiers}")
        return tiers

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"month": month, "tiers": tiers}, ensure_ascii=False, indent=2), encoding="utf-8")
    log_lines.append(f"[RSI-SWAP] セクターTier更新完了({month}): {tiers}")
    return tiers


def get_market_cap_tiers(universe_tickers: list[str], log_lines: list[str]) -> dict[str, int]:
    """渡されたティッカーの時価総額を取得し固定ラインTier(-1/0/+1)を返す（2026-10-07追加・毎晩。
    2026-10-08改訂で相対3分位→固定ライン(config.RSI_SWAP_MARKET_CAP_SMALL_MAX_USD/LARGE_MIN_USD)に変更。
    キャッシュしない＝呼ばれるたびmoomooから取り直す。get_market_snapshotはkline枠を消費しない。
    方向は2026-10-07改訂でJP枠と同じ小型株有利に変更＝config.RSI_SWAP_MARKET_CAP_FAVOR_SMALL）。
    """
    caps = broker.get_market_caps(universe_tickers)
    if caps is None:
        msg = "[RSI-SWAP] 警告: 時価総額の取得に失敗。Tierは全銘柄0として扱う"
        logger.warning(msg)
        log_lines.append(msg)
        return {}
    missing = len(universe_tickers) - len(caps)
    if missing:
        logger.warning("RSI-SWAP: 時価総額が取得できなかった銘柄 %d件", missing)
    return rsi_strategy.compute_market_cap_tiers(
        caps, config.RSI_SWAP_MARKET_CAP_SMALL_MAX_USD, config.RSI_SWAP_MARKET_CAP_LARGE_MIN_USD,
        favor_small_cap=config.RSI_SWAP_MARKET_CAP_FAVOR_SMALL,
    )


def build_dashboard_candidates(
    held_tickers: list[str], trade_date: str, log_lines: list[str],
    lots: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """ダッシュボード「今夜の候補」セクション（米国RSI枠）用データを組み立てる（2026-10-08追加）。

    frozen_candidates.jsonを直接読む（run()内のraw_candidatesは当日処理済みだと空になるため、
    表示専用のこちらは常に最新の確定候補を参照する）。発注・台帳は一切変更しない。
    frozen_candidates.jsonが無い・古い場合でも、保有一覧の規模表示は候補一覧と無関係なので
    held_tickers分の時価総額は引き続き取得する（候補一覧(candidates)だけ空にする）。
    セクターマップ・セクターTierの月次キャッシュ(sector_map.json/sector_tiers.json)は
    dry_run=True固定で呼ぶ（候補∪保有銘柄という一部ティッカーだけの取得結果で、
    その月の本来のスワップ判定用キャッシュ(全ユニバース分)を上書きする事故を防ぐため）。
    時価総額はget_market_cap_tiersと同じくキャッシュしない設計のため、この区別は不要。

    lots: state["lots"]（保有ロット一覧）。今夜買えない候補（保有中・利確前で再エントリー不可、
    または損切り後の再エントリー制限に抵触）をrun()と同じ判定関数(filter_blocked_entries・
    filter_stop_loss_reentries)で除外する（2026-10-09追加・Change1。大将「買えない銘柄は
    書かなくていいよ」。判定ロジックの二重実装を避けるため既存の純粋関数をそのまま再利用する）。
    省略時（Noneまたは--report-only等でまだ未取得の場合）は保有中チェックを一切行わない。
    """
    frozen = _load_frozen_candidates()
    if frozen is None:
        log_lines.append("[RSI-DASH] frozen_candidates.jsonが無いか古いため、候補セクションは空で表示する")
    raw_candidates = frozen["candidates"] if frozen else []

    # 損切り後の再エントリー制限（run()と同じ判定。candidateの価格はfrozen候補の価格=prior close）。
    stop_loss_history_all = rsi_strategy.latest_rule_closures(rsi_ledger.read_trade_rows())
    candidate_tickers_today = {c["ticker"] for c in raw_candidates}
    stop_loss_history = {t: sl for t, sl in stop_loss_history_all.items() if t in candidate_tickers_today}
    stop_loss_history = _split_adjusted_stop_loss_history(stop_loss_history, trade_date, log_lines)
    sl_check_candidates = [
        {"ticker": c["ticker"], "price": c["price"]} for c in raw_candidates if c["ticker"] in stop_loss_history
    ]
    _, sl_blocked = rsi_strategy.filter_stop_loss_reentries(sl_check_candidates, stop_loss_history, trade_date)
    sl_blocked_tickers = {b["ticker"] for b in sl_blocked}

    # 保有中・利確前の銘柄を抑止する（run()と同じ判定）。
    not_sl_blocked = [c for c in raw_candidates if c["ticker"] not in sl_blocked_tickers]
    candidates, blocked_entry_tickers = rsi_strategy.filter_blocked_entries(not_sl_blocked, lots or [])
    if sl_blocked_tickers or blocked_entry_tickers:
        log_lines.append(
            f"[RSI-DASH] 買えない候補を除外: 損切り後再エントリー制限={sorted(sl_blocked_tickers)} "
            f"保有中={blocked_entry_tickers}"
        )

    tickers = sorted({c["ticker"] for c in candidates} | set(held_tickers))
    caps = broker.get_market_caps(tickers) if tickers else {}
    if caps is None:
        log_lines.append("[RSI-DASH] 時価総額の取得に失敗。候補セクションのサイズ・スコアは不明(—)で表示する")
        caps = {}

    sector_of = get_sector_map(tickers, trade_date, dry_run=True, log_lines=log_lines) if tickers else {}
    sector_tiers = get_sector_tiers(trade_date, dry_run=True, log_lines=log_lines)
    mcap_tiers = rsi_strategy.compute_market_cap_tiers(
        caps, config.RSI_SWAP_MARKET_CAP_SMALL_MAX_USD, config.RSI_SWAP_MARKET_CAP_LARGE_MIN_USD,
        favor_small_cap=config.RSI_SWAP_MARKET_CAP_FAVOR_SMALL,
    )

    rows = []
    for c in candidates:
        t = c["ticker"]
        cap = caps.get(t)
        etf = sector_of.get(t)
        rows.append({
            "ticker": t,
            "name": c.get("name"),
            "rsi14": c.get("rsi14"),
            "market_cap": cap,
            "size_label": rsi_strategy.classify_market_cap_label(
                cap, config.RSI_SWAP_MARKET_CAP_SMALL_MAX_USD, config.RSI_SWAP_MARKET_CAP_LARGE_MIN_USD,
            ),
            "sector_tier": sector_tiers.get(etf) if etf else None,
            "score": rsi_strategy.compute_swap_score(t, sector_of, sector_tiers, mcap_tiers),
        })
    rows.sort(key=lambda r: (-r["score"], r["rsi14"]))
    return {"as_of": frozen["generated_at"] if frozen else None, "candidates": rows, "market_caps": caps}


def _compute_swap_scores(
    tickers: list[str], trade_date: str, dry_run: bool, log_lines: list[str],
) -> dict[str, int]:
    """スワップ判定に使うスコア（セクターTier+時価総額Tier）を対象ティッカー分まとめて計算する
    （2026-10-07追加）。セクターマップ・セクターTierは月次キャッシュ、時価総額Tierは毎晩
    ユニバース全体を取り直す（いずれもSPEC_RSI30.md「2026-10-07改訂」のスワップ売却仕様どおり）。
    """
    universe_tickers = universe.get_universe()
    sector_of = get_sector_map(universe_tickers, trade_date, dry_run, log_lines)
    sector_tiers = get_sector_tiers(trade_date, dry_run, log_lines)
    mcap_tiers = get_market_cap_tiers(universe_tickers, log_lines)
    return {
        t: rsi_strategy.compute_swap_score(t, sector_of, sector_tiers, mcap_tiers) for t in tickers
    }


def _run_swaps(
    state: dict[str, Any],
    unfunded_entries: list[dict[str, Any]],
    market_prices: dict[str, float],
    rsi_basis: str,
    market_us: str | None,
    trade_date: str,
    log_lines: list[str],
) -> list[dict[str, Any]]:
    """資金不足の新規エントリー候補を、保有ロットの入れ替え売りで拾えるか判定し発注する
    （2026-10-07追加。SPEC_RSI30.md「2026-10-07改訂」スワップ売却。do_trade=Trueの時のみ呼ばれる＝
    moomoo呼び出し・ファイル書き込みを行ってよい）。
    """
    sell_candidates = rsi_strategy.select_swap_sell_candidates(state["lots"], market_prices)
    if not sell_candidates:
        log_lines.append(f"[RSI-SWAP] 資金不足候補{len(unfunded_entries)}件だが売却可能なロットが無く入れ替え不可")
        return []

    tickers = sorted({c["ticker"] for c in unfunded_entries} | {lot["ticker"] for lot in sell_candidates})
    scores = _compute_swap_scores(tickers, trade_date, dry_run=False, log_lines=log_lines)
    cash = rsi_ledger.compute_available_cash(state, market_prices)
    decisions = rsi_strategy.decide_swaps(unfunded_entries, sell_candidates, scores, cash)
    if not decisions:
        log_lines.append(f"[RSI-SWAP] 資金不足候補{len(unfunded_entries)}件だが入れ替え条件を満たさず見送り")
        return []

    return _execute_swap_decisions(state, decisions, scores, rsi_basis, market_us, trade_date, log_lines)


def _execute_swap_decisions(
    state: dict[str, Any],
    decisions: list[dict[str, Any]],
    scores: dict[str, int],
    rsi_basis: str,
    market_us: str | None,
    trade_date: str,
    log_lines: list[str],
) -> list[dict[str, Any]]:
    """decide_swapsが決めたスワップ計画を実際に発注する（2026-10-07追加）。

    各決定について売りを先に全件発注し、全て全量約定(FULL)した場合のみ買いを行う（仕様「売りが
    約定しない夜は買わない」）。1件でも売りが全量約定しなければ、この決定もそれ以降の決定
    （他候補のスワップ）も中止する（decide_swapsの計画全体が売却見込み額の積み上げを前提に
    しているため、途中で崩れた時点でそれ以降の計画も前提が崩れている）。未決分は
    pending_ordersへ積み、既存のsettle_pending_ordersが次回実行時に解決する。
    """
    accepted: list[dict[str, Any]] = []
    for decision in decisions:
        buy = decision["buy"]
        sold_rows: list[dict[str, Any]] = []
        all_sells_full = True

        for sell in decision["sells"]:
            fill, cash_delta = _execute_order(sell["ticker"], int(sell["shares"]), "SELL", market_us)
            if fill is None:
                logger.warning("RSIスワップ売却発注失敗: %s lot=%s", sell["ticker"], sell["lot_id"])
                all_sells_full = False
                break

            outcome = broker.classify_fill(int(sell["shares"]), fill)
            filled_qty, avg_price = fill["filled_qty"], fill["avg_price"]

            if filled_qty > 0:
                idx = next(i for i, x in enumerate(state["lots"]) if x["lot_id"] == sell["lot_id"])
                lot_avg_cost = state["lots"][idx]["avg_cost"]
                realized_pnl, realized_pnl_pct = rsi_strategy.compute_realized_pnl(
                    lot_avg_cost, avg_price, filled_qty,
                )
                state["cash_usd"] += cash_delta
                state["lots"][idx] = rsi_strategy.apply_stop_loss_fill(
                    state["lots"][idx], filled_qty, trade_date, reason="swap",
                )
                trade_row = {
                    "date": trade_date, "action": "SELL", "ticker": sell["ticker"],
                    "shares": filled_qty, "price": round(avg_price, 4),
                    "amount_usd": round(filled_qty * avg_price, 2),
                    "rule": "swap", "lot_id": sell["lot_id"],
                    "note": f"入れ替え買い{buy['ticker']}のため(score={scores.get(sell['ticker'], 0)})",
                    "realized_pnl": realized_pnl, "realized_pnl_pct": realized_pnl_pct,
                    "name": state["lots"][idx].get("name"),
                }
                rsi_ledger.append_trade_row(trade_row)
                accepted.append(trade_row)
                sold_rows.append(sell)

            if outcome in ("PARTIAL_OPEN", "NONE_OPEN"):
                state.setdefault("pending_orders", [])
                state["pending_orders"].append({
                    "order_id": fill["order_id"], "ticker": sell["ticker"], "side": "SELL",
                    "qty": int(sell["shares"]), "submitted_date": trade_date,
                    "applied_qty": filled_qty, "applied_value_usd": filled_qty * avg_price,
                    "rule": "swap", "lot_id": sell["lot_id"],
                })
            if outcome != "FULL":
                all_sells_full = False
                break

        if not all_sells_full:
            logger.info("RSIスワップ中止: %s の入れ替え買いは見送り（売りが全量約定しなかった）", buy["ticker"])
            log_lines.append(f"[RSI-SWAP] {buy['ticker']}の入れ替え中止（売りが未達のため今夜は買わない）")
            break  # 仕様: 計画全体の前提が崩れるため、以降の決定も試みない

        fill, cash_delta = _execute_order(buy["ticker"], int(buy["qty"]), "BUY", market_us)
        if fill is None:
            logger.warning("RSIスワップ買い発注失敗: %s", buy["ticker"])
            log_lines.append(f"[RSI-SWAP] {buy['ticker']}の入れ替え買い発注に失敗した")
            break

        outcome = broker.classify_fill(int(buy["qty"]), fill)
        filled_qty, avg_price = fill["filled_qty"], fill["avg_price"]

        if outcome == "NONE_TERMINAL":
            logger.warning("RSIスワップ買い: 注文が約定せず終端した %s", buy["ticker"])
            log_lines.append(f"[RSI-SWAP] {buy['ticker']}の入れ替え買いが約定しなかった")
            continue

        lot_id = _new_lot_id(buy["ticker"], trade_date, state["lots"], state.get("pending_orders"))
        if filled_qty > 0:
            state["cash_usd"] += cash_delta
            new_lot = rsi_strategy.new_lot(buy["ticker"], lot_id, trade_date, filled_qty, avg_price, name=buy.get("name"))
            state["lots"].append(new_lot)
            trade_row = {
                "date": trade_date, "action": "BUY", "ticker": buy["ticker"],
                "shares": filled_qty, "price": round(avg_price, 4),
                "amount_usd": round(filled_qty * avg_price, 2),
                "rule": "entry", "lot_id": lot_id,
                "note": f"RSI14={buy['rsi14']:.1f} basis={rsi_basis} 入れ替え(score={scores.get(buy['ticker'], 0)})",
                "name": buy.get("name"),
            }
            rsi_ledger.append_trade_row(trade_row)
            accepted.append(trade_row)
            sold_desc = ", ".join(
                f"{s['ticker']}(score={scores.get(s['ticker'], 0)},含み損{(s['price'] / s['avg_cost'] - 1) * 100:.1f}%)"
                for s in sold_rows
            ) or "(売却なし)"
            log_lines.append(
                f"[RSI-SWAP] 入れ替え成立: {buy['ticker']}(score={scores.get(buy['ticker'], 0)}) ← 売却: {sold_desc}"
            )

        if outcome in ("PARTIAL_OPEN", "NONE_OPEN"):
            state.setdefault("pending_orders", [])
            state["pending_orders"].append({
                "order_id": fill["order_id"], "ticker": buy["ticker"], "side": "BUY",
                "qty": int(buy["qty"]), "submitted_date": trade_date,
                "applied_qty": filled_qty, "applied_value_usd": filled_qty * avg_price,
                "rule": "entry", "est_price": buy["price"], "name": buy.get("name"),
            })
        elif outcome == "PARTIAL_TERMINAL":
            logger.warning(
                "RSIスワップ買い: 一部約定(%d/%d株)のまま終端した %s lot=%s",
                filled_qty, buy["qty"], buy["ticker"], lot_id,
            )

    return accepted


def _preview_swaps(
    state: dict[str, Any],
    unfunded_entries: list[dict[str, Any]],
    market_prices: dict[str, float],
    trade_date: str,
    log_lines: list[str],
) -> None:
    """dry-run専用: 今夜もし売買するならどのスワップが決まるかをログに残すだけの関数
    （2026-10-07追加。発注・台帳変更・ファイル書き込みは一切行わない）。
    """
    sell_candidates = rsi_strategy.select_swap_sell_candidates(state["lots"], market_prices)
    if not sell_candidates:
        log_lines.append(f"[RSI-SWAP] [dry-run] 資金不足候補{len(unfunded_entries)}件だが売却可能なロットが無く入れ替え不可")
        return

    tickers = sorted({c["ticker"] for c in unfunded_entries} | {lot["ticker"] for lot in sell_candidates})
    scores = _compute_swap_scores(tickers, trade_date, dry_run=True, log_lines=log_lines)
    cash = rsi_ledger.compute_available_cash(state, market_prices)
    decisions = rsi_strategy.decide_swaps(unfunded_entries, sell_candidates, scores, cash)
    if not decisions:
        log_lines.append(f"[RSI-SWAP] [dry-run] 資金不足候補{len(unfunded_entries)}件だが入れ替え条件を満たさず見送り予定")
        return

    for decision in decisions:
        buy = decision["buy"]
        sold_desc = ", ".join(
            f"{s['ticker']}(score={scores.get(s['ticker'], 0)})" for s in decision["sells"]
        ) or "(売却なし)"
        log_lines.append(
            f"[RSI-SWAP] [dry-run予告] {buy['ticker']}(score={scores.get(buy['ticker'], 0)}) ← 売却: {sold_desc}"
        )


def compute_snapshot_only(
    rsi_state: dict[str, Any], voo_snap: TickerSnapshot,
) -> tuple[float, float, dict[str, TickerSnapshot]]:
    """保有銘柄の価格だけを取得してNAV/ベンチマークを計算する（ユニバース走査・売買は行わない）。

    --report-only・異常停止時など、表示更新のみが必要な場面で使う。
    """
    held_tickers = sorted({lot["ticker"] for lot in rsi_ledger.open_lots(rsi_state)})
    market = fetch_market_data(held_tickers) if held_tickers else {}
    market_snapshots = {
        t: TickerSnapshot(ticker=t, close=info["close"], date=info["date"])
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
    market_us: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str], float, float, dict[str, TickerSnapshot]]:
    """RSI-30枠の1日分の処理を行う。

    未決注文の決済（settle_pending_orders）は、本体決済と合わせて手数料を口座全体の現金増減から
    逆算するためdaily_run.py側で先に呼ばれる（2026-08-18 修正3。渡されるrsi_stateは決済済み）。

    market_us: daily_run.py側が1回の実行につき1回だけ問い合わせたbroker.get_market_us_state()の
    戻り値。損切り・利確・買い増し・新規エントリーの全発注経路（_execute_order）にそのまま渡す
    （2026-08-18 修正2）。

    戻り値: (更新後のrsi_state, 約定した取引ログ, ログ用メッセージ行, NAV, ベンチマーク評価額, 保有銘柄の市場スナップショット)
    """
    log_lines: list[str] = []
    accepted_trades: list[dict[str, Any]] = []

    state = dict(rsi_state)
    state["lots"] = [dict(lot) for lot in rsi_state.get("lots", [])]

    # 初回構築（本体のstart_date/bench_units構築と同じ扱い。何度呼ばれても1回しか発火しない）
    if state["start_date"] is None:
        state["start_date"] = voo_snap.date
        state["bench_units_rsi"] = config.RSI_INITIAL_CAPITAL_USD / voo_snap.close
        log_lines.append(f"[RSI-0] 初回構築: start_date={state['start_date']} bench_units_rsi={state['bench_units_rsi']:.6f}")

    # 候補はfrozen_candidates.jsonが新しければそれを使う（rsi_basis="prev_close"）。
    # その場合、候補のRSIは信頼するが価格は古い可能性があるため、株数計算に使う価格は
    # get_market_snapshotで執行時の最新値を取り直す（2026-08-19改修1-b。大将「株価は執行時の最新値」）。
    # frozen候補が無い・古い場合はscreen_rsi_candidates()を今叩く（rsi_basis="live"）。
    # この場合はget_market_snapshot済みの戻り値をそのまま使い、価格取得を二重に行わない。
    # いずれの経路でも、重複銘柄（保有中の再エントリー候補）は保有側の値を優先する。
    # 判定（損切り・利確・買い増し）とNAV計算の前に株式分割を反映する（2026-10-01）
    adjust_lots_for_splits(state, trade_date, log_lines)

    # 配当記帳（2026-10-07追加・Change3）。ファイルI/Oを含むためdry-runでは呼ばない
    # （daily_run.py全体の「dry-runはledger/配下を一切変更しない」契約を保つ）。
    if not dry_run:
        credit_dividends(state, log_lines)

    held_tickers = sorted({lot["ticker"] for lot in rsi_ledger.open_lots(state)})
    rsi_candidates, rsi_basis = get_rsi_candidates()
    if rsi_basis == "prev_close":
        candidate_tickers = [c["ticker"] for c in rsi_candidates]
        candidate_market = fetch_market_data(candidate_tickers) if candidate_tickers else {}
    else:
        candidate_market = {
            c["ticker"]: {"close": c["price"], "date": c["date"]} for c in rsi_candidates
        }
    held_market = fetch_market_data(held_tickers) if held_tickers else {}
    market = {**candidate_market, **held_market}
    market_prices = {t: v["close"] for t, v in market.items()}
    log_lines.append(
        f"[RSI-1] 候補{len(rsi_candidates)}銘柄(basis={rsi_basis})・保有{len(held_tickers)}銘柄・価格取得{len(market)}銘柄"
    )

    # 損切り後の再エントリー制限（2026-10-07追加。SPEC_RSI30.md「2026-10-07改訂」参照）。
    # candidateの価格はfrozen候補の価格（c["price"]・prior close）を使う。執行用に再取得した
    # market[...]["close"]ではない（大将「q1) 1」＝RSI通常候補であることに加え価格条件を見る）。
    stop_loss_history_all = rsi_strategy.latest_rule_closures(rsi_ledger.read_trade_rows())
    candidate_tickers_today = {c["ticker"] for c in rsi_candidates}
    stop_loss_history = {t: sl for t, sl in stop_loss_history_all.items() if t in candidate_tickers_today}
    stop_loss_history = _split_adjusted_stop_loss_history(stop_loss_history, trade_date, log_lines)
    sl_check_candidates = [
        {"ticker": c["ticker"], "price": c["price"]} for c in rsi_candidates if c["ticker"] in stop_loss_history
    ]
    _, sl_blocked = rsi_strategy.filter_stop_loss_reentries(sl_check_candidates, stop_loss_history, trade_date)
    sl_blocked_tickers = {b["ticker"] for b in sl_blocked}
    for b in sl_blocked:
        logger.info(
            "RSI新規エントリー見送り(損切り後の再エントリー制限): %s 候補価格が閾値%.4f(損切り価格%.4f×0.85)"
            "を上回り、損切り日%sから%d営業日しか経過していない",
            b["ticker"], b["threshold"], b["stop_loss_price"], b["stop_loss_date"], b["trading_days_elapsed"],
        )
    if sl_blocked_tickers:
        log_lines.append(
            f"[RSI-1] 新規エントリー見送り(損切り後の再エントリー制限): {', '.join(sorted(sl_blocked_tickers))}"
        )

    # 新規エントリー候補から、保有中・利確前の銘柄を抑止する（2026-08-19改修1。大将「１だな」）。
    # dry-runでも現金が余った理由が追えるよう、実行判断（do_trade）とは独立に必ず計算・ログする。
    raw_entry_candidates = [
        {"ticker": c["ticker"], "rsi14": c["rsi14"], "price": market[c["ticker"]]["close"], "name": c.get("name")}
        for c in rsi_candidates
        if c["ticker"] in market and c["ticker"] not in sl_blocked_tickers
    ]
    entry_candidates, blocked_entry_tickers = rsi_strategy.filter_blocked_entries(raw_entry_candidates, state["lots"])
    for ticker in blocked_entry_tickers:
        logger.info("RSI新規エントリー見送り(保有中のため): %s", ticker)
    if blocked_entry_tickers:
        log_lines.append(f"[RSI-1] 新規エントリー見送り(保有中のため): {', '.join(blocked_entry_tickers)}")

    # dry-run専用のスワップ売却プレビュー（2026-10-07追加）。do_trade=Falseのため本番実行では
    # 通らない経路（下のdo_trade内で改めて正式に判定・発注する）。ファイル書き込みは行わない。
    if dry_run:
        preview_cash = rsi_ledger.compute_available_cash(state, market_prices)
        _, preview_unfunded = rsi_strategy.select_entries_with_unfunded(entry_candidates, preview_cash)
        if preview_unfunded:
            _preview_swaps(state, preview_unfunded, market_prices, trade_date, log_lines)

    do_trade = can_trade and not already_processed_today and not dry_run

    if do_trade:
        # --- 1. 損切り・利確（SELL群を先に処理して現金を作る） ---
        for lot in sorted(state["lots"], key=lambda x: (x["ticker"], x["lot_id"])):
            if lot.get("closed"):
                continue
            info = market.get(lot["ticker"])
            if info is None:
                logger.warning("RSI: %s の価格が取得できずロット%sの判定をスキップした", lot["ticker"], lot["lot_id"])
                continue
            price = info["close"]
            if not rsi_strategy.is_valid_price(price):
                logger.warning("RSI: %s の価格が不正(%r)のためロット%sの判定をスキップした", lot["ticker"], price, lot["lot_id"])
                continue
            idx = next(i for i, x in enumerate(state["lots"]) if x["lot_id"] == lot["lot_id"])

            stop = rsi_strategy.decide_stop_loss(state["lots"][idx], price)
            if stop is not None:
                fill, cash_delta = _execute_order(stop["ticker"], stop["qty"], "SELL", market_us)
                if fill is None:
                    logger.warning("RSI損切り発注失敗: %s lot=%s", stop["ticker"], stop["lot_id"])
                    continue

                outcome = broker.classify_fill(stop["qty"], fill)
                filled_qty, avg_price = fill["filled_qty"], fill["avg_price"]

                if outcome == "NONE_TERMINAL":
                    logger.warning(
                        "RSI損切り: 注文が約定せず終端した lot=%s status=%s", stop["lot_id"], fill["status"],
                    )
                    continue

                if filled_qty > 0:
                    realized_pnl, realized_pnl_pct = rsi_strategy.compute_realized_pnl(
                        lot["avg_cost"], avg_price, filled_qty,
                    )
                    state["cash_usd"] += cash_delta
                    state["lots"][idx] = rsi_strategy.apply_stop_loss_fill(state["lots"][idx], filled_qty, trade_date)
                    trade_row = {
                        "date": trade_date, "action": "SELL", "ticker": stop["ticker"],
                        "shares": filled_qty, "price": round(avg_price, 4),
                        "amount_usd": round(filled_qty * avg_price, 2),
                        "rule": "stop_loss", "lot_id": stop["lot_id"], "note": "",
                        "realized_pnl": realized_pnl, "realized_pnl_pct": realized_pnl_pct,
                        "name": state["lots"][idx].get("name"),
                    }
                    rsi_ledger.append_trade_row(trade_row)
                    accepted_trades.append(trade_row)

                if outcome in ("PARTIAL_OPEN", "NONE_OPEN"):
                    state.setdefault("pending_orders", [])
                    state["pending_orders"].append({
                        "order_id": fill["order_id"], "ticker": stop["ticker"], "side": "SELL",
                        "qty": stop["qty"], "submitted_date": trade_date,
                        "applied_qty": filled_qty, "applied_value_usd": filled_qty * avg_price,
                        "rule": "stop_loss", "lot_id": stop["lot_id"],
                    })
                elif outcome == "PARTIAL_TERMINAL":
                    logger.warning(
                        "RSI損切り: 一部約定(%d/%d株)のまま終端した lot=%s", filled_qty, stop["qty"], stop["lot_id"],
                    )
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

                fill, cash_delta = _execute_order(intent["ticker"], intent["qty"], "SELL", market_us)
                if fill is None:
                    logger.warning("RSI利確発注失敗: %s lot=%s kind=%s", intent["ticker"], intent["lot_id"], intent["kind"])
                    break

                outcome = broker.classify_fill(intent["qty"], fill)
                filled_qty, avg_price = fill["filled_qty"], fill["avg_price"]

                if outcome == "NONE_TERMINAL":
                    logger.warning(
                        "RSI利確: 注文が約定せず終端した lot=%s kind=%s status=%s",
                        intent["lot_id"], intent["kind"], fill["status"],
                    )
                    break

                if filled_qty > 0:
                    realized_pnl, realized_pnl_pct = rsi_strategy.compute_realized_pnl(
                        lot["avg_cost"], avg_price, filled_qty,
                    )
                    state["cash_usd"] += cash_delta
                    if intent["kind"] == "profit1":
                        state["lots"][idx] = rsi_strategy.apply_profit1_fill(
                            state["lots"][idx], filled_qty, intent["base_shares"],
                        )
                    else:
                        state["lots"][idx] = rsi_strategy.apply_profit2_fill(state["lots"][idx], filled_qty)
                    trade_row = {
                        "date": trade_date, "action": "SELL", "ticker": intent["ticker"],
                        "shares": filled_qty, "price": round(avg_price, 4),
                        "amount_usd": round(filled_qty * avg_price, 2),
                        "rule": intent["kind"], "lot_id": intent["lot_id"], "note": "",
                        "realized_pnl": realized_pnl, "realized_pnl_pct": realized_pnl_pct,
                        "name": state["lots"][idx].get("name"),
                    }
                    rsi_ledger.append_trade_row(trade_row)
                    accepted_trades.append(trade_row)

                if outcome in ("PARTIAL_OPEN", "NONE_OPEN"):
                    pending_entry = {
                        "order_id": fill["order_id"], "ticker": intent["ticker"], "side": "SELL",
                        "qty": intent["qty"], "submitted_date": trade_date,
                        "applied_qty": filled_qty, "applied_value_usd": filled_qty * avg_price,
                        "rule": intent["kind"], "lot_id": intent["lot_id"],
                    }
                    if intent["kind"] == "profit1":
                        pending_entry["base_shares"] = intent["base_shares"]
                    state.setdefault("pending_orders", [])
                    state["pending_orders"].append(pending_entry)
                    break  # 未決分の帰結が付くまで、このロットへの追加判定は次回実行に持ち越す

                if outcome == "PARTIAL_TERMINAL":
                    logger.warning(
                        "RSI利確: 一部約定(%d/%d株)のまま終端した lot=%s kind=%s",
                        filled_qty, intent["qty"], intent["lot_id"], intent["kind"],
                    )
                # outcome == FULL、またはPARTIAL_TERMINAL処理後はループ先頭に戻り次の利確条件を判定する

        # --- 2. 買い増し（新規エントリーより優先） ---
        for lot in sorted(state["lots"], key=lambda x: (x["ticker"], x["lot_id"])):
            if lot.get("closed"):
                continue
            info = market.get(lot["ticker"])
            if info is None:
                continue
            price = info["close"]
            if not rsi_strategy.is_valid_price(price):
                logger.warning("RSI: %s の価格が不正(%r)のためロット%sの判定をスキップした", lot["ticker"], price, lot["lot_id"])
                continue
            idx = next(i for i, x in enumerate(state["lots"]) if x["lot_id"] == lot["lot_id"])
            for intent in rsi_strategy.decide_pyramid_buys(state["lots"][idx], price):
                qty = rsi_strategy.qty_for_amount(intent["amount_usd"], price, lot.get("lot_size", 1))
                if qty <= 0:
                    continue
                estimated_cost = qty * price
                available_cash = rsi_ledger.compute_available_cash(state, market_prices)
                if estimated_cost > available_cash + 1e-6:
                    logger.info("RSI買い増しスキップ（現金不足）: %s %s", intent["ticker"], intent["kind"])
                    continue
                fill, cash_delta = _execute_order(intent["ticker"], qty, "BUY", market_us)
                if fill is None:
                    logger.warning("RSI買い増し発注失敗: %s lot=%s kind=%s", intent["ticker"], intent["lot_id"], intent["kind"])
                    continue

                outcome = broker.classify_fill(qty, fill)
                filled_qty, avg_price = fill["filled_qty"], fill["avg_price"]

                if outcome == "NONE_TERMINAL":
                    logger.warning(
                        "RSI買い増し: 注文が約定せず終端した lot=%s kind=%s status=%s",
                        intent["lot_id"], intent["kind"], fill["status"],
                    )
                    continue

                if filled_qty > 0:
                    state["cash_usd"] += cash_delta
                    state["lots"][idx] = rsi_strategy.apply_pyramid_fill(
                        state["lots"][idx], intent["stage_index"], filled_qty, avg_price,
                    )
                    trade_row = {
                        "date": trade_date, "action": "BUY", "ticker": intent["ticker"],
                        "shares": filled_qty, "price": round(avg_price, 4),
                        "amount_usd": round(filled_qty * avg_price, 2),
                        "rule": intent["kind"], "lot_id": intent["lot_id"], "note": "",
                        "name": state["lots"][idx].get("name"),
                    }
                    rsi_ledger.append_trade_row(trade_row)
                    accepted_trades.append(trade_row)

                if outcome in ("PARTIAL_OPEN", "NONE_OPEN"):
                    state.setdefault("pending_orders", [])
                    state["pending_orders"].append({
                        "order_id": fill["order_id"], "ticker": intent["ticker"], "side": "BUY",
                        "qty": qty, "submitted_date": trade_date,
                        "applied_qty": filled_qty, "applied_value_usd": filled_qty * avg_price,
                        "rule": intent["kind"], "lot_id": intent["lot_id"], "stage_index": intent["stage_index"],
                        "est_price": price,
                    })
                elif outcome == "PARTIAL_TERMINAL":
                    logger.warning(
                        "RSI買い増し: 一部約定(%d/%d株)のまま終端した lot=%s kind=%s",
                        filled_qty, qty, intent["lot_id"], intent["kind"],
                    )

        # --- 3. 新規エントリー（RSIが低い順。現金が足りる分だけ。保有中・利確前は抑止済み） ---
        selected, unfunded_entries = rsi_strategy.select_entries_with_unfunded(
            entry_candidates, rsi_ledger.compute_available_cash(state, market_prices),
        )
        for cand in selected:
            fill, cash_delta = _execute_order(cand["ticker"], cand["qty"], "BUY", market_us)
            if fill is None:
                logger.warning("RSI新規エントリー発注失敗: %s", cand["ticker"])
                continue

            outcome = broker.classify_fill(cand["qty"], fill)
            filled_qty, avg_price = fill["filled_qty"], fill["avg_price"]

            if outcome == "NONE_TERMINAL":
                logger.warning("RSI新規エントリー: 注文が約定せず終端した %s status=%s", cand["ticker"], fill["status"])
                continue

            lot_id = _new_lot_id(cand["ticker"], trade_date, state["lots"], state.get("pending_orders"))

            if filled_qty > 0:
                state["cash_usd"] += cash_delta
                new_lot = rsi_strategy.new_lot(cand["ticker"], lot_id, trade_date, filled_qty, avg_price, name=cand.get("name"))
                state["lots"].append(new_lot)
                trade_row = {
                    "date": trade_date, "action": "BUY", "ticker": cand["ticker"],
                    "shares": filled_qty, "price": round(avg_price, 4),
                    "amount_usd": round(filled_qty * avg_price, 2),
                    "rule": "entry", "lot_id": lot_id, "note": f"RSI14={cand['rsi14']:.1f} basis={rsi_basis}",
                    "name": cand.get("name"),
                }
                rsi_ledger.append_trade_row(trade_row)
                accepted_trades.append(trade_row)

            if outcome in ("PARTIAL_OPEN", "NONE_OPEN"):
                # entryロットはまだ存在しない（0株のロットを作らない）。settle_pending_ordersが後で生成する
                state.setdefault("pending_orders", [])
                state["pending_orders"].append({
                    "order_id": fill["order_id"], "ticker": cand["ticker"], "side": "BUY",
                    "qty": cand["qty"], "submitted_date": trade_date,
                    "applied_qty": filled_qty, "applied_value_usd": filled_qty * avg_price,
                    "rule": "entry", "lot_id": lot_id, "est_price": cand["price"], "name": cand.get("name"),
                })
            elif outcome == "PARTIAL_TERMINAL":
                logger.warning(
                    "RSI新規エントリー: 一部約定(%d/%d株)のまま終端した %s lot=%s",
                    filled_qty, cand["qty"], cand["ticker"], lot_id,
                )

        # --- 4. スワップ売却（資金不足の候補を保有ロットの入れ替え売りで拾う。2026-10-07追加。
        #     市場が開いていない場合は_execute_order内部のガードが各注文を自然に見送る） ---
        if unfunded_entries:
            swap_trades = _run_swaps(
                state, unfunded_entries, market_prices, rsi_basis, market_us, trade_date, log_lines,
            )
            accepted_trades.extend(swap_trades)

        state["last_processed_date"] = trade_date
        log_lines.append(f"[RSI-2] 約定{len(accepted_trades)}件（pending決済/損切り/利確/買い増し/新規エントリー/スワップ込み）")
    else:
        reason = "dry-run" if dry_run else ("休場/処理済み" if already_processed_today else "売買停止中")
        log_lines.append(f"[RSI-2] 売買スキップ（{reason}）")

    market_snapshots = {
        t: TickerSnapshot(ticker=t, close=info["close"], date=info["date"])
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


def _setup_freeze_logging() -> None:
    """--freeze-candidates単独起動用のログ設定（daily_run.py経由では呼ばれない）。

    日本株RSI枠(jp_rsi_daily)のログも同じfreeze_candidates.logへ出す（2026-08-24追加。
    新しいlaunchdジョブは作らずこの20:00ジョブに相乗りするため、ログも1本にまとめる）。
    """
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    if not logger.handlers:
        fh = logging.FileHandler(config.LOG_DIR / "freeze_candidates.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(fh)
        logger.setLevel(logging.INFO)
    if not jp_rsi_daily.logger.handlers:
        jp_fh = logging.FileHandler(config.LOG_DIR / "freeze_candidates.log", encoding="utf-8")
        jp_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        jp_rsi_daily.logger.addHandler(jp_fh)
        jp_rsi_daily.logger.setLevel(logging.INFO)


def _send_freeze_failure_alert(us_failed: bool, jp_failed: bool) -> None:
    """20:00の候補確定が最終的に失敗した時、保留を待たず即時Telegramで知らせる
    （2026-09-02実測: moomoo未接続で3回失敗してもログに残すだけで誰にも通知されなかった）。"""
    import report

    if us_failed and jp_failed:
        waku = "米国枠・JP枠"
    elif us_failed:
        waku = "米国枠"
    else:
        waku = "JP枠"
    message = (
        f"⚠️ vs-sp500: 20:00の候補確定がmoomoo未接続で失敗した（{waku}）。OpenDが落ちとる。"
        "今夜02:00のdaily_runまでにmoomooデスクトップでOpenDを起動・ログインしてくれ。"
    )
    prev = os.environ.pop("VS_SP500_DEFER_TELEGRAM", None)
    try:
        report.send_telegram_message(message)
    except Exception:
        logger.error("freeze失敗の即時Telegramアラート送信に失敗した", exc_info=True)
    finally:
        if prev is not None:
            os.environ["VS_SP500_DEFER_TELEGRAM"] = prev


def main() -> None:
    """単独起動エントリポイント（2026-08-19改修1-a）。現状は--freeze-candidatesのみ。

    2026-08-24: 日本株RSI枠の候補確定もここに相乗りさせる（新しいlaunchdジョブは作らない。
    大将の指示「20:00の候補確定ジョブに日本株の候補確定を追加する」）。米国枠とJP枠は互いに
    独立しているため、片方が失敗してももう片方は試行する。
    """
    parser = argparse.ArgumentParser(description="vs-sp500 RSI枠の単独ジョブ")
    parser.add_argument(
        "--freeze-candidates", action="store_true",
        help="screen_rsi_candidates()を1回呼び、結果をfrozen_candidates.jsonに保存する（発注・台帳変更なし）。"
             "日本株RSI枠の候補確定も同時に行う",
    )
    args = parser.parse_args()
    if not args.freeze_candidates:
        parser.print_help()
        return

    _setup_freeze_logging()

    result = freeze_candidates()
    if result is None:
        print("候補確定に失敗した（moomoo未接続）")
        # 2026-09-05: アラートは枠ごとに最終失敗の直後に送る。9/4実測でJP側の再試行が6時間ハングし、
        # 末尾でまとめて送る旧設計では「即時」アラートが02:17になった
        _send_freeze_failure_alert(us_failed=True, jp_failed=False)
    else:
        print(
            f"候補確定完了: {len(result['candidates'])}件 rsi_basis={result['rsi_basis']} "
            f"generated_at={result['generated_at']}"
        )

    jp_result = jp_rsi_daily.freeze_candidates_jp()
    if jp_result is None:
        print("JP候補確定に失敗した（moomoo未接続）")
        _send_freeze_failure_alert(us_failed=False, jp_failed=True)
    else:
        print(f"JP候補確定完了: {len(jp_result['candidates'])}件 generated_at={jp_result['generated_at']}")

    exit_code = 1 if (result is None or jp_result is None) else 0
    # moomoo SDKの非デーモンスレッド（OpenD未応答時の永久再接続）が残ってもプロセスを確実に終了させる。
    # daily_run.pyには2026-09-02に同じ対策を入れたがこちらは漏れとった。
    # 2026-09-21実測: Mac再起動後OpenDが未ログインのまま、20:00の候補確定が3回失敗した後も
    # 6時間以上生き残り（22スレッド・再接続1万3千回・ログ7MB）、launchdの翌20:00起動を塞ぐ状態だった。
    logging.shutdown()
    os._exit(exit_code)


if __name__ == "__main__":
    main()
