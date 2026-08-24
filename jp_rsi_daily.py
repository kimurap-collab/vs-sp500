"""vs-sp500: 日本株RSI枠の毎日実行ロジック（3本目の戦略枠。2026-08-24追加）。

大将の発言（2026-08-24）:
  「じゃあ日本株もやってみようか。rsi35-30までの銘柄を仮想購入。予算１億円。
   時価総額300億円以上の企業。株価は買う瞬間にyahoofinanceでも見に行けばいいだろう。
   厳密さは不要」「ルールは「RSI-30枠 vs S&P500」と同じで良い」

米国RSI-32枠(rsi_daily.py)とは以下が異なる:
- 円建て・ベンチマーク無し（元本比の損益だけを見る）
- **moomooへの発注は一切行わない。台帳の上だけの仮想売買**（moomoo日本の仮想口座が
  存在しないため。moomoo.jp_stock_qot_right=NOで日本株の相場取得・request_history_klineも
  権限エラーになることを2026-08-24実機確認済み）。約定価格はその日の終値をそのまま使う。
  → **この枠だけ、他の2枠と違って実際の発注による執行の裏付けが無い。**
- 候補抽出はmoomooスクリーナー(get_stock_filter, market=JP)が権限不要で使え、
  RSI・株価(CUR_PRICE)・時価総額(MARKET_VAL)を1回の呼び出しで取得できることを実機確認済み。
- 保有銘柄の日々の価格はyfinance（スクリーナーはRSI<=35に該当する銘柄しか返さないため、
  保有中にRSIが35を超えて外れた銘柄の価格が取れなくなるのを避けるため）。
- 単元株数(lot_size)はget_stock_basicinfo(Market.JP)でキャッシュ取得し、月初のみ更新。
  1単元の金額が予算(300万円)を超える銘柄・lot_sizeが取得できない銘柄はスキップしログに残す。

売買ルールそのもの（エントリー・買い増し・利確・伸ばす玉・15営業日/56日例外・
同一銘柄1ロット制限・資金不足時の優先順位）はrsi_strategy.pyを共用する
（rules=rsi_strategy.JP_RULESを渡すだけで、ロジックの複製は一切していない）。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
from typing import Any, Callable

import broker
import config
import jp_lotsize
import jp_market
import jp_rsi_ledger
import rsi_strategy
from jp_market import JpSnapshot

logger = logging.getLogger("vs-sp500.jp_rsi_daily")

_MOOMOO_CALL_TIMEOUT_SEC = 15.0  # broker.pyのCALL_TIMEOUT_SECに合わせる
_MOOMOO_SCREENER_PAGE_SIZE = 200  # moomoo 1リクエストの最大件数（rsi_daily.pyと同じ）


def _run_with_timeout(fn: Callable[[], Any], timeout: float = _MOOMOO_CALL_TIMEOUT_SEC) -> Any | None:
    """broker.pyと同じ方式: デーモンスレッドで実行しtimeoutで見切りをつける（rsi_daily.pyと同型）。"""
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


def _sleep_seconds(seconds: float) -> None:
    """time.sleepの薄いラッパー（テストでリトライ待ちをモックできるようにするため）。"""
    time.sleep(seconds)


def screen_jp_candidates() -> list[dict[str, Any]] | None:
    """moomooスクリーナーでRSI(14)<=35 かつ 時価総額300億円以上の日本株を抽出する（RSI昇順）。

    RSI・株価・時価総額を1回のget_stock_filter呼び出しで取得する（2026-08-24実機確認:
    SimpleFilterをis_no_filter=Trueで追加すると、絞り込みはせず値だけが結果に含まれる）。
    RelativePosition.LESSはUS版と同じく「未満」（<=を直接表現するAPIオプションが無いため。
    RSIは連続値のため実運用上の差は無視できる。米国RSI-32枠でも同じ近似を使っている）。

    戻り値: moomoo接続・取得に失敗した場合はNone。該当銘柄が0件なら空リスト。
    """

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
            rsi_filter.value = config.RSI_JP_ENTRY_RSI_THRESHOLD
            rsi_filter.relative_position = RelativePosition.LESS
            rsi_filter.is_no_filter = False

            cap_filter = SimpleFilter()
            cap_filter.stock_field = StockField.MARKET_VAL
            cap_filter.filter_min = config.RSI_JP_SCREENER_MIN_MARKET_CAP_JPY
            cap_filter.is_no_filter = False

            price_filter = SimpleFilter()
            price_filter.stock_field = StockField.CUR_PRICE
            price_filter.is_no_filter = True  # 絞り込みはせず、値だけを結果に含める

            rows: list[Any] = []
            begin = 0
            while True:
                ret, ret_data = ctx.get_stock_filter(
                    market=Market.JP, filter_list=[rsi_filter, cap_filter, price_filter],
                    begin=begin, num=_MOOMOO_SCREENER_PAGE_SIZE,
                )
                if ret != 0:
                    raise RuntimeError(f"get_stock_filter失敗: {ret_data}")
                last_page, all_count, ret_list = ret_data
                rows.extend(ret_list)
                if last_page or not ret_list:
                    break
                logger.warning(
                    "JPスクリーナー: last_pageがFalseのためページを繰る（取得済み%d件 / 全%d件）",
                    len(rows), all_count,
                )
                begin += len(ret_list)
            return rows
        finally:
            ctx.close()

    rows = _run_with_timeout(_call)
    if rows is None:
        logger.error("JPスクリーナー(get_stock_filter)の呼び出しに失敗した")
        return None

    screened: list[dict[str, Any]] = []
    for row in rows:
        d = row.__dict__
        code = str(d.get("stock_code", ""))
        ticker = code[3:] if code.startswith("JP.") else code
        rsi_val = d.get(("rsi", "14", "k_day"))
        price = d.get("cur_price")
        market_cap = d.get("market_val")
        if rsi_val is None or not price:
            continue
        screened.append({
            "ticker": ticker,
            "rsi14": float(rsi_val),
            "price": float(price),
            "market_cap": float(market_cap) if market_cap is not None else None,
        })
    screened.sort(key=lambda c: c["rsi14"])
    return screened


def freeze_candidates_jp() -> dict[str, Any] | None:
    """JP候補を20:00の候補確定ジョブで確定しfrozen_candidates.jsonへ保存する（発注・台帳変更なし）。

    日本市場は現地14:30に引けているため、20:00(JST)時点の値は常にその日の日本の終値になる
    （米国RSI枠のrsi_basis="prev_close"/"live"の区別は不要）。
    リトライ設定は米国RSI枠と共用の定数（RSI_FREEZE_CANDIDATES_MAX_ATTEMPTS/RETRY_DELAYS_SEC）を使う。
    """
    max_attempts = config.RSI_FREEZE_CANDIDATES_MAX_ATTEMPTS
    candidates: list[dict[str, Any]] | None = None
    for attempt in range(1, max_attempts + 1):
        if not broker.is_available():
            logger.error("JP候補確定 試行%d/%d回目: moomoo未接続", attempt, max_attempts)
        else:
            candidates = screen_jp_candidates()
            if candidates is not None:
                break
            logger.error("JP候補確定 試行%d/%d回目: 候補の取得に失敗した", attempt, max_attempts)

        if attempt < max_attempts:
            delay = config.RSI_FREEZE_CANDIDATES_RETRY_DELAYS_SEC[attempt - 1]
            logger.warning("JP候補確定: %.0f秒待って再試行する", delay)
            _sleep_seconds(delay)

    if candidates is None:
        logger.error("JP候補確定: %d回試行しても取得できなかったため中止した", max_attempts)
        return None

    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "candidates": candidates,
    }
    config.RSI_JP_LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    config.RSI_JP_FROZEN_CANDIDATES_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    logger.info("JP候補確定完了: %d件 → %s", len(candidates), config.RSI_JP_FROZEN_CANDIDATES_PATH)
    return payload


def _load_frozen_candidates_jp() -> dict[str, Any] | None:
    """JP frozen_candidates.jsonを読む。無い・壊れている・12時間より古ければNoneを返す。"""
    path = config.RSI_JP_FROZEN_CANDIDATES_PATH
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        generated_at = dt.datetime.fromisoformat(payload["generated_at"])
    except (OSError, ValueError, KeyError) as e:
        logger.warning("JP frozen_candidates.jsonの読み込みに失敗した: %s", e)
        return None
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=dt.timezone.utc)
    age_hours = (dt.datetime.now(dt.timezone.utc) - generated_at).total_seconds() / 3600
    if age_hours > config.RSI_JP_FROZEN_CANDIDATES_MAX_AGE_HOURS:
        logger.warning("JP frozen_candidates.jsonが%.1f時間前と古いため無視する", age_hours)
        return None
    return payload


def get_jp_candidates() -> list[dict[str, Any]]:
    """執行時に使うJP候補を返す。frozenが無い・古い場合は空リスト（新規エントリーを見送るのみ。

    米国RSI枠と異なりmoomoo発注が無い純粋な台帳更新のため、その場での再スクリーニングによる
    フェイルオープンは行わない。既存ロットの利確・買い増しはyfinance価格で通常どおり継続する）。
    """
    frozen = _load_frozen_candidates_jp()
    if frozen is None:
        logger.warning("JP frozen_candidates.jsonが無いか古いため、本日は新規エントリーを見送る")
        return []
    return frozen["candidates"]


def build_entry_candidates(
    candidates: list[dict[str, Any]],
    lot_sizes: dict[str, int],
    entry_amount: float = config.RSI_JP_ENTRY_AMOUNT_JPY,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """frozen候補にlot_sizeを付与し、買えない銘柄を分ける（純粋関数。ログはしない）。

    「1単元の金額が予算を超える銘柄は買えない。スキップしてログに残すこと」
    「lot_sizeが取得できない銘柄は買わずにログに残す（推測で買わない）」（SPEC）。

    戻り値: (買える候補にlot_sizeを付加したリスト, 1単元が予算超過でスキップした候補,
             lot_size不明でスキップしたticker一覧)
    """
    allowed: list[dict[str, Any]] = []
    too_expensive: list[dict[str, Any]] = []
    no_lotsize: list[str] = []
    for c in candidates:
        lot_size = lot_sizes.get(c["ticker"])
        if lot_size is None:
            no_lotsize.append(c["ticker"])
            continue
        unit_cost = lot_size * c["price"]
        if unit_cost > entry_amount:
            too_expensive.append({**c, "lot_size": lot_size, "unit_cost": unit_cost})
            continue
        allowed.append({**c, "lot_size": lot_size})
    return allowed, too_expensive, no_lotsize


def _new_lot_id_jp(ticker: str, entry_date: str, existing_lots: list[dict[str, Any]]) -> str:
    """新しいlot_idを発番する（JP枠は未決注文が無いためpending分の予約は不要）。"""
    seq = sum(1 for lot in existing_lots if lot["ticker"] == ticker)
    return f"{ticker}-{entry_date}-{seq + 1}"


def compute_snapshot_only_jp(jp_state: dict[str, Any]) -> tuple[float, dict[str, JpSnapshot]]:
    """保有銘柄の価格だけを取得してNAVを計算する（--report-only・異常停止時用）。"""
    held_tickers = sorted({lot["ticker"] for lot in jp_rsi_ledger.open_lots(jp_state)})
    market = jp_market.get_snapshots(held_tickers) if held_tickers else {}
    nav_jpy = jp_rsi_ledger.compute_nav_jpy(jp_state, market)
    return nav_jpy, market


def run_jp(
    jp_state: dict[str, Any], trading_date: str, dry_run: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str], float, dict[str, JpSnapshot]]:
    """日本株RSI枠の1日分の処理を行う。

    **moomooへの発注は行わない（日本の仮想口座が存在しないため）。台帳の上だけの仮想売買**。
    約定価格はその日の終値をそのまま使う（新規エントリーはfrozen候補の株価＝20:00確定の
    当日終値、既存ロットの判定はyfinanceの当日終値）。手数料はモデル化しない（ゼロ）。
    処理順序は米国RSI枠と同じ: 利確（SELL）→買い増し→新規エントリー。

    戻り値: (更新後のjp_state, 約定した取引ログ, ログ用メッセージ行, NAV(円), 保有銘柄の市場スナップショット)
    """
    log_lines: list[str] = []
    accepted_trades: list[dict[str, Any]] = []

    state = dict(jp_state)
    state["lots"] = [dict(lot) for lot in jp_state.get("lots", [])]

    if state["start_date"] is None:
        state["start_date"] = trading_date
        log_lines.append(f"[JP-0] 初回構築: start_date={state['start_date']}")

    already_processed_today = state.get("last_processed_date") == trading_date

    held_tickers = sorted({lot["ticker"] for lot in jp_rsi_ledger.open_lots(state)})
    raw_candidates = [] if already_processed_today else get_jp_candidates()
    candidate_prices = {c["ticker"]: c["price"] for c in raw_candidates}
    held_market = jp_market.get_snapshots(held_tickers) if held_tickers else {}
    market_prices = {**candidate_prices, **{t: s.close for t, s in held_market.items()}}
    log_lines.append(
        f"[JP-1] 候補{len(raw_candidates)}銘柄・保有{len(held_tickers)}銘柄・価格取得{len(market_prices)}銘柄"
    )

    lot_sizes = jp_lotsize.get_lot_sizes()

    raw_entry_candidates = [c for c in raw_candidates if c["ticker"] in market_prices]
    entry_candidates0, blocked_entry_tickers = rsi_strategy.filter_blocked_entries(raw_entry_candidates, state["lots"])
    entry_candidates, too_expensive, no_lotsize = build_entry_candidates(entry_candidates0, lot_sizes)
    for ticker in blocked_entry_tickers:
        log_lines.append(f"[JP-1] 新規エントリー見送り(保有中のため): {ticker}")
    for c in too_expensive:
        log_lines.append(
            f"[JP-1] 新規エントリー見送り(1単元が予算超過): {c['ticker']} lot_size={c['lot_size']} "
            f"単元金額={c['unit_cost']:,.0f}円 > {config.RSI_JP_ENTRY_AMOUNT_JPY:,.0f}円"
        )
    for ticker in no_lotsize:
        log_lines.append(f"[JP-1] 新規エントリー見送り(lot_size不明): {ticker}")

    do_trade = not already_processed_today and not dry_run

    if do_trade:
        # --- 1. 利確（SELL群を先に処理して現金を作る） ---
        for lot in sorted(state["lots"], key=lambda x: (x["ticker"], x["lot_id"])):
            if lot.get("closed"):
                continue
            price = market_prices.get(lot["ticker"])
            if price is None:
                logger.warning("JP: %s の価格が取得できずロット%sの判定をスキップした", lot["ticker"], lot["lot_id"])
                continue
            idx = next(i for i, x in enumerate(state["lots"]) if x["lot_id"] == lot["lot_id"])
            trading_days_elapsed = rsi_strategy.business_days_since(lot["initial_entry_date"], trading_date)
            while True:
                intents = rsi_strategy.decide_profit_takes(
                    state["lots"][idx], price, trading_date, trading_days_elapsed, rsi_strategy.JP_RULES,
                )
                if not intents:
                    break
                intent = intents[0]
                if intent["kind"] == "exception_trigger":
                    state["lots"][idx] = rsi_strategy.apply_exception_trigger(state["lots"][idx], rsi_strategy.JP_RULES)
                    break
                qty = intent["qty"]
                if intent["kind"] == "profit1":
                    state["lots"][idx] = rsi_strategy.apply_profit1_fill(state["lots"][idx], qty, intent["base_shares"])
                else:
                    state["lots"][idx] = rsi_strategy.apply_profit2_fill(state["lots"][idx], qty)
                state["cash_jpy"] += qty * price
                trade_row = {
                    "date": trading_date, "action": "SELL", "ticker": intent["ticker"],
                    "shares": qty, "price": round(price, 2), "amount_jpy": round(qty * price, 0),
                    "rule": intent["kind"], "lot_id": intent["lot_id"],
                    "note": "moomoo発注なし・台帳のみの仮想売買",
                }
                jp_rsi_ledger.append_trade_row(trade_row)
                accepted_trades.append(trade_row)

        # --- 2. 買い増し（新規エントリーより優先） ---
        for lot in sorted(state["lots"], key=lambda x: (x["ticker"], x["lot_id"])):
            if lot.get("closed"):
                continue
            price = market_prices.get(lot["ticker"])
            if price is None:
                continue
            idx = next(i for i, x in enumerate(state["lots"]) if x["lot_id"] == lot["lot_id"])
            for intent in rsi_strategy.decide_pyramid_buys(state["lots"][idx], price, rsi_strategy.JP_RULES):
                lot_size = state["lots"][idx].get("lot_size", 1)
                qty = rsi_strategy.qty_for_amount(intent["amount_usd"], price, lot_size)
                if qty <= 0:
                    log_lines.append(f"[JP-2] 買い増し見送り(単元未満): {intent['ticker']} {intent['kind']}")
                    continue
                cost = qty * price
                if cost > state["cash_jpy"] + 1e-6:
                    log_lines.append(f"[JP-2] 買い増し見送り(現金不足): {intent['ticker']} {intent['kind']}")
                    continue
                state["lots"][idx] = rsi_strategy.apply_pyramid_fill(
                    state["lots"][idx], intent["stage_index"], qty, price,
                )
                state["cash_jpy"] -= cost
                trade_row = {
                    "date": trading_date, "action": "BUY", "ticker": intent["ticker"],
                    "shares": qty, "price": round(price, 2), "amount_jpy": round(cost, 0),
                    "rule": intent["kind"], "lot_id": intent["lot_id"],
                    "note": "moomoo発注なし・台帳のみの仮想売買",
                }
                jp_rsi_ledger.append_trade_row(trade_row)
                accepted_trades.append(trade_row)

        # --- 3. 新規エントリー（RSIが低い順。現金が足りる分だけ。保有中・利確前は抑止済み） ---
        selected = rsi_strategy.select_entries_within_cash(entry_candidates, state["cash_jpy"], rsi_strategy.JP_RULES)
        for cand in selected:
            lot_id = _new_lot_id_jp(cand["ticker"], trading_date, state["lots"])
            new_lot = rsi_strategy.new_lot(
                cand["ticker"], lot_id, trading_date, cand["qty"], cand["price"], cand["lot_size"],
            )
            state["lots"].append(new_lot)
            cost = cand["qty"] * cand["price"]
            state["cash_jpy"] -= cost
            trade_row = {
                "date": trading_date, "action": "BUY", "ticker": cand["ticker"],
                "shares": cand["qty"], "price": round(cand["price"], 2), "amount_jpy": round(cost, 0),
                "rule": "entry", "lot_id": lot_id,
                "note": f"RSI14={cand['rsi14']:.1f} lot_size={cand['lot_size']}・moomoo発注なし・台帳のみの仮想売買",
            }
            jp_rsi_ledger.append_trade_row(trade_row)
            accepted_trades.append(trade_row)

        state["last_processed_date"] = trading_date
        log_lines.append(f"[JP-2] 約定{len(accepted_trades)}件（利確/買い増し/新規エントリー込み）")
    else:
        reason = "dry-run" if dry_run else "処理済み"
        log_lines.append(f"[JP-2] 売買スキップ（{reason}）")

    # NAV計算用の市場スナップショット: 保有銘柄（元々の保有＋今回新規に建てた分）を全てカバーする。
    # 新規に建てた銘柄はcandidate_pricesにしか価格が無い（held_marketは今回のトレード前の保有分のみ）ため、
    # 両方をマージしないと新規建て分の評価額が0円のまま計上されない事故になる。
    market_snapshots: dict[str, JpSnapshot] = dict(held_market)
    for lot in jp_rsi_ledger.open_lots(state):
        t = lot["ticker"]
        if t not in market_snapshots and t in candidate_prices:
            market_snapshots[t] = JpSnapshot(ticker=t, close=candidate_prices[t], date=trading_date)

    nav_jpy = jp_rsi_ledger.compute_nav_jpy(state, market_snapshots)
    principal = config.RSI_JP_INITIAL_CAPITAL_JPY
    diff_jpy = nav_jpy - principal
    cash_ratio = jp_rsi_ledger.compute_cash_ratio(state, nav_jpy) if nav_jpy else 0.0
    log_lines.append(f"[JP-3] 評価額: NAV=¥{nav_jpy:,.0f} 元本比=¥{diff_jpy:,.0f}")

    if not dry_run:
        jp_rsi_ledger.append_history_row({
            "date": trading_date,
            "nav_jpy": round(nav_jpy, 0),
            "principal_jpy": round(principal, 0),
            "diff_jpy": round(diff_jpy, 0),
            "diff_pct": round(diff_jpy / principal * 100, 4) if principal else 0.0,
            "cash_ratio": round(cash_ratio, 4),
            "open_lots": len(jp_rsi_ledger.open_lots(state)),
        })
        jp_rsi_ledger.save_portfolio(state)
        log_lines.append("[JP-4] 台帳保存完了")
    else:
        log_lines.append("[JP-4] dry-runのため台帳保存はスキップ")

    return state, accepted_trades, log_lines, nav_jpy, market_snapshots
