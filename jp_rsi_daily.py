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
  lot_sizeが取得できない銘柄はスキップしログに残す。1単元の金額が予算(300万円)を超える
  値がさ株は2026-08-27改訂で「買える最大単元数（最低1単元）を買う」に変更（見送りはしない）。
- 「会社の株のみ」ルール（2026-08-27・大将「reitは除外せよ」）: get_stock_basicinfoの
  SecurityType.STOCKに載っている銘柄のみ新規エントリー対象とする。載っていない銘柄
  （J-REIT等はETF区分でのみ返る）は「会社の株ではない」として除外しログに残す。

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
import dividends
import jp_lotsize
import jp_market
import jp_rsi_ledger
import jp_sector_map
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
            "name": d.get("stock_name"),
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


def filter_non_company_entries(
    candidates: list[dict[str, Any]], company_tickers: set[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """新規エントリー候補から「会社の株」でない銘柄（J-REIT等）を除外する（純粋関数。ログはしない）。

    大将「会社しか買うなと言ってるだろ。1000億円で足切りできたのは結果であってルールとは
    違っている。reitは除外せよ」（2026-08-27）。company_tickersはjp_lotsize.get_company_tickers()
    （moomoo get_stock_basicinfo(SecurityType.STOCK)の実機確認に基づく「会社の株」一覧）。

    戻り値: (会社の株のみの候補リスト, 除外したticker一覧)
    """
    allowed = [c for c in candidates if c["ticker"] in company_tickers]
    excluded = [c["ticker"] for c in candidates if c["ticker"] not in company_tickers]
    return allowed, excluded


def build_entry_candidates(
    candidates: list[dict[str, Any]],
    lot_sizes: dict[str, int],
) -> tuple[list[dict[str, Any]], list[str]]:
    """frozen候補にlot_sizeを付与する（純粋関数。ログはしない）。

    「lot_sizeが取得できない銘柄は買わずにログに残す（推測で買わない）」（SPEC）。
    1単元の金額が予算(300万円)を超える銘柄は、2026-08-27改訂により
    「買える最大単元数（最低1単元）を買う」に変更されたためここではスキップしない
    （実際のサイジングはrsi_strategy.select_entries_within_cashが行う）。

    戻り値: (lot_sizeを付加した候補リスト, lot_size不明でスキップしたticker一覧)
    """
    allowed: list[dict[str, Any]] = []
    no_lotsize: list[str] = []
    for c in candidates:
        lot_size = lot_sizes.get(c["ticker"])
        if lot_size is None:
            no_lotsize.append(c["ticker"])
            continue
        allowed.append({**c, "lot_size": lot_size})
    return allowed, no_lotsize


def _new_lot_id_jp(ticker: str, entry_date: str, existing_lots: list[dict[str, Any]]) -> str:
    """新しいlot_idを発番する（JP枠は未決注文が無いためpending分の予約は不要）。"""
    seq = sum(1 for lot in existing_lots if lot["ticker"] == ticker)
    return f"{ticker}-{entry_date}-{seq + 1}"


def adjust_lots_for_splits_jp(state: dict[str, Any], trading_date: str, log_lines: list[str]) -> None:
    """保有ロットに建て後の株式分割があれば、判定の前に株数・単価を分割後の値へ直す（2026-10-01追加）。

    大将「分割は分割できちんと計算しないとね。」。9/29に3099(1:2)・9065(1:5)の分割を暴落と誤認し
    -8%損切りが誤発動した事故の再発防止。分割情報はyfinance（この枠は価格もyfinance）。
    取得に失敗した銘柄はWARNINGを出して調整せずに続行する（実行は止めない）。state["lots"]を置き換える。
    """
    held_tickers = sorted({lot["ticker"] for lot in jp_rsi_ledger.open_lots(state)})
    splits_by_ticker: dict[str, list[tuple[str, float]]] = {}
    for ticker in held_tickers:
        splits = jp_market.get_splits(ticker)
        if splits is None:
            logger.warning("JP: %s の分割情報が取得できず、分割調整なしで判定する", ticker)
            continue
        splits_by_ticker[ticker] = splits
    new_lots = []
    for lot in state["lots"]:
        new_lot, applied = rsi_strategy.adjust_lot_for_splits(
            lot, splits_by_ticker.get(lot["ticker"], []), trading_date,
        )
        if applied:
            splits_text = ", ".join(f"{d} 1:{r:g}" for d, r in applied)
            msg = (
                f"[JP-0] 株式分割を反映: {lot['ticker']} ({splits_text}) "
                f"株数{lot['shares']}→{new_lot['shares']} 初期単価{lot['initial_entry_price']:.2f}→"
                f"{new_lot['initial_entry_price']:.2f}"
            )
            logger.warning(msg)
            log_lines.append(msg)
        new_lots.append(new_lot)
    state["lots"] = new_lots


def _split_adjusted_stop_loss_history_jp(
    stop_loss_history: dict[str, dict[str, Any]], trading_date: str, log_lines: list[str],
) -> dict[str, dict[str, Any]]:
    """損切り後の再エントリー制限で使う売却価格Pを、売却日より後の株式分割があれば調整する
    （2026-10-07追加）。分割情報はyfinance（adjust_lots_for_splits_jpと同じ取得元）。
    取得に失敗した銘柄はWARNINGを出し、分割調整なし（元の価格のまま）で続行する（実行は止めない）。
    """
    adjusted: dict[str, dict[str, Any]] = {}
    for ticker, sl in stop_loss_history.items():
        splits = jp_market.get_splits(ticker)
        if splits is None:
            logger.warning("JP: %s の分割情報が取得できず、損切り再エントリー判定は分割調整なしで続行する", ticker)
            adjusted[ticker] = sl
            continue
        new_price = rsi_strategy.adjust_stop_loss_price_for_splits(sl["price"], sl["date"], splits, trading_date)
        adjusted[ticker] = {**sl, "price": new_price}
    return adjusted


def credit_dividends_jp(state: dict[str, Any], log_lines: list[str]) -> None:
    """保有中・過去保有ロットの配当をcash_jpyへ記帳する（2026-10-07追加・Change3）。

    yfinance（この枠は価格もyfinance）から配当履歴を取得し、未記帳の
    (ticker, ex_date, lot_id)だけをledger/rsi_jp/dividends.csvへ追記してcash_jpyに加算する。
    呼び出し側（run_jpのdry_runガード）がdry-runでは呼ばない前提（ファイルI/Oを含むため）。
    個別銘柄の取得失敗はWARNINGを出して記帳なしで続行する（実行は止めない）。
    """
    held_tickers = sorted({lot["ticker"] for lot in jp_rsi_ledger.open_lots(state)})
    if not held_tickers:
        return
    dividends_by_ticker: dict[str, list[tuple[str, float]]] = {}
    for ticker in held_tickers:
        divs = jp_market.get_dividends(ticker)
        if divs is None:
            logger.warning("JP: %s の配当情報が取得できず、配当記帳なしで続行する", ticker)
            continue
        dividends_by_ticker[ticker] = divs
    existing_keys = {(r["ticker"], r["date"], r["lot_id"]) for r in jp_rsi_ledger.read_dividend_rows()}
    trades = jp_rsi_ledger.read_trade_rows()
    new_rows = dividends.compute_new_dividend_credits(
        state["lots"], trades, dividends_by_ticker, existing_keys, source="yfinance",
    )
    for row in new_rows:
        jp_rsi_ledger.append_dividend_row(row)
        state["cash_jpy"] += row["amount"]
        log_lines.append(
            f"[JP-DIV] {row['ticker']} lot={row['lot_id']} {row['date']} "
            f"{row['shares']}株×¥{row['per_share']:.2f} = ¥{row['amount']:.0f}"
        )


def _month_key(date_str: str) -> str:
    return date_str[:7]  # "YYYY-MM"


# ---------------------------------------------------------------------------
# スワップ売却（2026-10-07追加。SPEC_RSI30.md「2026-10-07改訂（JP枠）」参照。米国RSI枠の
# スワップ売却機能をJP枠にも追加したもの。判定ロジック(decide_swaps等)はrsi_strategy.pyを
# そのまま共用し、ここではJP固有のデータ取得（yfinance・moomooスクリーナー）と
# 台帳のみの仮想売買（moomoo発注なし）を行う。
# ---------------------------------------------------------------------------

def get_sector_map_jp(
    universe_tickers: list[str], trading_date: str, dry_run: bool, log_lines: list[str],
) -> dict[str, str | None]:
    """ticker→TOPIX-17セクターコード（不明はNone）のマッピングをledger/rsi_jp/sector_map.jsonに
    キャッシュする（2026-10-07追加。米国枠のget_sector_mapと同じ月次周期・dry-run時は保存しない契約）。

    分類はyfinanceの.info（sector/industry）をjp_sector_map.classify_infoに渡して行う
    （moomooのget_owner_plateに相当する発注不要の一括APIがJP株には無いため、1銘柄ずつ
    yfinanceへ問い合わせる。月1回しか呼ばないため許容する）。個別銘柄の取得失敗はNone
    （Tier0として扱う）。
    """
    path = config.RSI_JP_SWAP_SECTOR_MAP_PATH
    month = _month_key(trading_date)
    cache: dict[str, Any] | None = None
    if path.exists():
        try:
            cache = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning("JP sector_map.json読み込み失敗: %s", e)
            cache = None
    if cache is not None and cache.get("month") == month:
        return cache["map"]

    new_map: dict[str, str | None] = {}
    for ticker in universe_tickers:
        info = jp_market.get_info(ticker)
        new_map[ticker] = jp_sector_map.classify_info(info, ticker)

    if dry_run:
        log_lines.append(f"[JP-SWAP] [dry-run] sector_map更新対象だが保存はスキップ（{len(universe_tickers)}銘柄・{month}）")
        return new_map

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"month": month, "map": new_map}, ensure_ascii=False, indent=2), encoding="utf-8")
    log_lines.append(f"[JP-SWAP] sector_map更新完了: {len(universe_tickers)}銘柄 ({month})")
    return new_map


def get_sector_tiers_jp(trading_date: str, dry_run: bool, log_lines: list[str]) -> dict[str, int]:
    """TOPIX-17シリーズETF17本の直近21営業日リターンから月次のセクターTier(-1/0/+1)を
    ledger/rsi_jp/sector_tiers.jsonにキャッシュする（2026-10-07追加。lookback日数は米国枠と
    同じconfig.RSI_SWAP_SECTOR_RETURN_LOOKBACK_TRADING_DAYSを共用する＝JP専用の値は作らない）。
    """
    path = config.RSI_JP_SWAP_SECTOR_TIERS_PATH
    month = _month_key(trading_date)
    cache: dict[str, Any] | None = None
    if path.exists():
        try:
            cache = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning("JP sector_tiers.json読み込み失敗: %s", e)
            cache = None
    if cache is not None and cache.get("month") == month:
        return cache["tiers"]

    returns = jp_market.get_sector_etf_returns(
        config.RSI_JP_SWAP_SECTOR_ETFS, config.RSI_SWAP_SECTOR_RETURN_LOOKBACK_TRADING_DAYS,
    )
    if not returns or len(returns) < len(config.RSI_JP_SWAP_SECTOR_ETFS):
        msg = "[JP-SWAP] 警告: セクターTier更新に失敗（ETFリターン取得不足）。既存キャッシュ(無ければ全セクター0)で続行"
        logger.warning(msg)
        log_lines.append(msg)
        return (cache or {}).get("tiers", {})

    tiers = rsi_strategy.rank_sector_etf_tiers(returns)
    if dry_run:
        log_lines.append(f"[JP-SWAP] [dry-run] セクターTier更新対象だが保存はスキップ（{month}）: {tiers}")
        return tiers

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"month": month, "tiers": tiers}, ensure_ascii=False, indent=2), encoding="utf-8")
    log_lines.append(f"[JP-SWAP] セクターTier更新完了({month}): {tiers}")
    return tiers


def get_market_cap_tiers_jp(
    raw_candidates: list[dict[str, Any]], held_tickers: list[str], log_lines: list[str],
) -> dict[str, int]:
    """JP候補ユニバース(raw_candidates)∪保有銘柄(held_tickers)の時価総額から固定ラインTierを求める
    （2026-10-07追加・大将「5)1」の日本株版＝小型株は時価総額Tierで不利、のFableによる代替案＝
    小型株を有利にする。5年分のバックテストで10/10勝ち越しを確認し「いけ」で承認済み。
    2026-10-07改訂で米国枠もこの方向に揃えたため、向きはconfig.RSI_JP_SWAP_MARKET_CAP_FAVOR_SMALL
    で指定するだけで済み、米国枠との差は無くなった。2026-10-08改訂で相対3分位→固定ライン
    (config.RSI_JP_SWAP_MARKET_CAP_SMALL_MAX_JPY/LARGE_MIN_JPY)に変更）。
    毎晩実行しキャッシュしない（米国枠と同じ方針）。

    時価総額データは2系統を使う:
      - raw_candidatesの"market_cap"（moomooスクリーナーが1回の呼び出しで既に返している値。
        追加のAPI呼び出し不要）。
      - raw_candidatesに無い保有銘柄（当夜RSIが35を超えて候補から外れた銘柄等）はyfinance
        （jp_market.get_market_caps）で個別に補う。
    """
    caps: dict[str, float] = {
        c["ticker"]: c["market_cap"] for c in raw_candidates if c.get("market_cap")
    }
    missing = [t for t in held_tickers if t not in caps]
    if missing:
        caps.update(jp_market.get_market_caps(missing))

    if not caps:
        msg = "[JP-SWAP] 警告: 時価総額が1件も取得できずTierは全銘柄0として扱う"
        logger.warning(msg)
        log_lines.append(msg)
        return {}

    return rsi_strategy.compute_market_cap_tiers(
        caps, config.RSI_JP_SWAP_MARKET_CAP_SMALL_MAX_JPY, config.RSI_JP_SWAP_MARKET_CAP_LARGE_MIN_JPY,
        favor_small_cap=config.RSI_JP_SWAP_MARKET_CAP_FAVOR_SMALL,
    )


def build_dashboard_candidates_jp(
    held_tickers: list[str], trading_date: str, log_lines: list[str],
) -> dict[str, Any]:
    """ダッシュボード「今夜の候補」セクション（日本株RSI枠）用データを組み立てる（2026-10-08追加）。

    frozen_candidates.jsonを直接読む（run_jp()内のraw_candidatesは当日処理済みだと空になるため、
    表示専用のこちらは常に最新の確定候補を参照する）。台帳は一切変更しない。
    frozen_candidates.jsonが無い・古い場合でも、保有一覧の規模表示は候補一覧と無関係なので
    held_tickers分の時価総額（yfinance）は引き続き取得する（候補一覧(candidates)だけ空にする）。
    セクターマップ・セクターTierの月次キャッシュはdry_run=True固定で呼ぶ（米国枠と同じ理由。
    get_market_cap_tiers_jpのTier計算ロジックを参照）。
    """
    frozen = _load_frozen_candidates_jp()
    if frozen is None:
        log_lines.append("[JP-DASH] frozen_candidates.jsonが無いか古いため、候補セクションは空で表示する")
    candidates = frozen["candidates"] if frozen else []
    caps: dict[str, float] = {
        c["ticker"]: c["market_cap"] for c in candidates if c.get("market_cap")
    }
    missing = [t for t in held_tickers if t not in caps]
    if missing:
        caps.update(jp_market.get_market_caps(missing))

    tickers = sorted({c["ticker"] for c in candidates} | set(held_tickers))
    sector_of = get_sector_map_jp(tickers, trading_date, dry_run=True, log_lines=log_lines) if tickers else {}
    sector_tiers = get_sector_tiers_jp(trading_date, dry_run=True, log_lines=log_lines)
    mcap_tiers = rsi_strategy.compute_market_cap_tiers(
        caps, config.RSI_JP_SWAP_MARKET_CAP_SMALL_MAX_JPY, config.RSI_JP_SWAP_MARKET_CAP_LARGE_MIN_JPY,
        favor_small_cap=config.RSI_JP_SWAP_MARKET_CAP_FAVOR_SMALL,
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
                cap, config.RSI_JP_SWAP_MARKET_CAP_SMALL_MAX_JPY, config.RSI_JP_SWAP_MARKET_CAP_LARGE_MIN_JPY,
            ),
            "sector_tier": sector_tiers.get(etf) if etf else None,
            "score": rsi_strategy.compute_swap_score(t, sector_of, sector_tiers, mcap_tiers),
        })
    rows.sort(key=lambda r: (-r["score"], r["rsi14"]))
    return {"as_of": frozen["generated_at"] if frozen else None, "candidates": rows, "market_caps": caps}


def _compute_swap_scores_jp(
    raw_candidates: list[dict[str, Any]],
    held_tickers: list[str],
    score_tickers: list[str],
    trading_date: str,
    dry_run: bool,
    log_lines: list[str],
) -> dict[str, int]:
    """スワップ判定用スコア（セクターTier＋時価総額Tier）を
    score_tickers分まとめて計算する（2026-10-07追加。時価総額Tierの向きは当初米国枠と逆だったが、
    2026-10-07改訂で米国枠もJP枠と同じ小型株有利に揃えたため差は無い）。

    Tierの母集団は「その夜の候補(raw_candidates)∪保有銘柄(held_tickers)」（JP枠には米国枠の
    universe.jsonに相当する固定ユニバースが無いため、moomooスクリーナーが返す当夜のRSI<=35
    候補集合をそのまま母集団として使う＝仕様「tertiles across the JP candidate universe/holdings
    the frame uses」どおり）。
    """
    universe_tickers = sorted({c["ticker"] for c in raw_candidates} | set(held_tickers))
    sector_of = get_sector_map_jp(universe_tickers, trading_date, dry_run, log_lines)
    sector_tiers = get_sector_tiers_jp(trading_date, dry_run, log_lines)
    mcap_tiers = get_market_cap_tiers_jp(raw_candidates, held_tickers, log_lines)
    return {
        t: rsi_strategy.compute_swap_score(t, sector_of, sector_tiers, mcap_tiers) for t in score_tickers
    }


def _run_swaps_jp(
    state: dict[str, Any],
    unfunded_entries: list[dict[str, Any]],
    raw_candidates: list[dict[str, Any]],
    market_prices: dict[str, float],
    trading_date: str,
    log_lines: list[str],
) -> list[dict[str, Any]]:
    """資金不足の新規エントリー候補を、保有ロットの入れ替え売りで拾えるか判定し台帳へ反映する
    （2026-10-07追加。moomoo発注は一切行わない台帳のみの仮想売買。売り・買いともその日の
    終値で即時約定として扱うため、米国枠にある「売りが未達なら買わない」分岐は無い）。
    """
    accepted: list[dict[str, Any]] = []
    sell_candidates = rsi_strategy.select_swap_sell_candidates(state["lots"], market_prices)
    if not sell_candidates:
        log_lines.append(f"[JP-SWAP] 資金不足候補{len(unfunded_entries)}件だが売却可能なロットが無く入れ替え不可")
        return accepted

    held_tickers = sorted({lot["ticker"] for lot in jp_rsi_ledger.open_lots(state)})
    score_tickers = sorted({c["ticker"] for c in unfunded_entries} | {lot["ticker"] for lot in sell_candidates})
    scores = _compute_swap_scores_jp(
        raw_candidates, held_tickers, score_tickers, trading_date, dry_run=False, log_lines=log_lines,
    )
    decisions = rsi_strategy.decide_swaps(unfunded_entries, sell_candidates, scores, state["cash_jpy"])
    if not decisions:
        log_lines.append(f"[JP-SWAP] 資金不足候補{len(unfunded_entries)}件だが入れ替え条件を満たさず見送り")
        return accepted

    for decision in decisions:
        buy = decision["buy"]
        sold_rows: list[dict[str, Any]] = []
        for sell in decision["sells"]:
            idx = next(i for i, x in enumerate(state["lots"]) if x["lot_id"] == sell["lot_id"])
            qty = int(sell["shares"])
            price = sell["price"]
            realized_pnl, realized_pnl_pct = rsi_strategy.compute_realized_pnl(sell["avg_cost"], price, qty)
            state["lots"][idx] = rsi_strategy.apply_stop_loss_fill(state["lots"][idx], qty, trading_date, reason="swap")
            state["cash_jpy"] += qty * price
            trade_row = {
                "date": trading_date, "action": "SELL", "ticker": sell["ticker"],
                "shares": qty, "price": round(price, 2), "amount_jpy": round(qty * price, 0),
                "rule": "swap", "lot_id": sell["lot_id"],
                "realized_pnl": realized_pnl, "realized_pnl_pct": realized_pnl_pct,
                "note": (
                    f"入れ替え買い{buy['ticker']}のため(score={scores.get(sell['ticker'], 0)})・"
                    "moomoo発注なし・台帳のみの仮想売買"
                ),
                "name": state["lots"][idx].get("name"),
            }
            jp_rsi_ledger.append_trade_row(trade_row)
            accepted.append(trade_row)
            sold_rows.append(sell)

        lot_id = _new_lot_id_jp(buy["ticker"], trading_date, state["lots"])
        new_lot = rsi_strategy.new_lot(
            buy["ticker"], lot_id, trading_date, buy["qty"], buy["price"], buy["lot_size"], name=buy.get("name"),
        )
        state["lots"].append(new_lot)
        cost = buy["qty"] * buy["price"]
        state["cash_jpy"] -= cost
        trade_row = {
            "date": trading_date, "action": "BUY", "ticker": buy["ticker"],
            "shares": buy["qty"], "price": round(buy["price"], 2), "amount_jpy": round(cost, 0),
            "rule": "entry", "lot_id": lot_id,
            "note": (
                f"RSI14={buy['rsi14']:.1f} lot_size={buy['lot_size']}・"
                f"入れ替え(score={scores.get(buy['ticker'], 0)})・moomoo発注なし・台帳のみの仮想売買"
            ),
            "name": buy.get("name"),
        }
        jp_rsi_ledger.append_trade_row(trade_row)
        accepted.append(trade_row)

        sold_desc = ", ".join(
            f"{s['ticker']}(score={scores.get(s['ticker'], 0)},含み損{(s['price'] / s['avg_cost'] - 1) * 100:.1f}%)"
            for s in sold_rows
        ) or "(売却なし)"
        log_lines.append(
            f"[JP-SWAP] 入れ替え成立: {buy['ticker']}(score={scores.get(buy['ticker'], 0)}) ← 売却: {sold_desc}"
        )

    return accepted


def _preview_swaps_jp(
    state: dict[str, Any],
    unfunded_entries: list[dict[str, Any]],
    raw_candidates: list[dict[str, Any]],
    market_prices: dict[str, float],
    trading_date: str,
    log_lines: list[str],
) -> None:
    """dry-run専用: 今夜もし売買するならどのスワップが決まるかをログに残すだけの関数
    （2026-10-07追加。台帳変更・ファイル書き込みは一切行わない）。
    """
    sell_candidates = rsi_strategy.select_swap_sell_candidates(state["lots"], market_prices)
    if not sell_candidates:
        log_lines.append(f"[JP-SWAP] [dry-run] 資金不足候補{len(unfunded_entries)}件だが売却可能なロットが無く入れ替え不可")
        return

    held_tickers = sorted({lot["ticker"] for lot in jp_rsi_ledger.open_lots(state)})
    score_tickers = sorted({c["ticker"] for c in unfunded_entries} | {lot["ticker"] for lot in sell_candidates})
    scores = _compute_swap_scores_jp(
        raw_candidates, held_tickers, score_tickers, trading_date, dry_run=True, log_lines=log_lines,
    )
    decisions = rsi_strategy.decide_swaps(unfunded_entries, sell_candidates, scores, state["cash_jpy"])
    if not decisions:
        log_lines.append(f"[JP-SWAP] [dry-run] 資金不足候補{len(unfunded_entries)}件だが入れ替え条件を満たさず見送り予定")
        return

    for decision in decisions:
        buy = decision["buy"]
        sold_desc = ", ".join(
            f"{s['ticker']}(score={scores.get(s['ticker'], 0)})" for s in decision["sells"]
        ) or "(売却なし)"
        log_lines.append(
            f"[JP-SWAP] [dry-run予告] {buy['ticker']}(score={scores.get(buy['ticker'], 0)}) ← 売却: {sold_desc}"
        )


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
    処理順序は米国RSI枠と同じ: 損切り→利確（SELL）→買い増し→新規エントリー。

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

    # 判定（損切り・利確・買い増し）とNAV計算の前に株式分割を反映する（2026-10-01）
    adjust_lots_for_splits_jp(state, trading_date, log_lines)

    # 配当記帳（2026-10-07追加・Change3）。ファイルI/Oを含むためdry-runでは呼ばない
    # （daily_run.py全体の「dry-runはledger/配下を一切変更しない」契約を保つ）。
    if not dry_run:
        credit_dividends_jp(state, log_lines)

    held_tickers = sorted({lot["ticker"] for lot in jp_rsi_ledger.open_lots(state)})
    raw_candidates = [] if already_processed_today else get_jp_candidates()
    candidate_prices = {c["ticker"]: c["price"] for c in raw_candidates}
    held_market = jp_market.get_snapshots(held_tickers) if held_tickers else {}
    market_prices = {**candidate_prices, **{t: s.close for t, s in held_market.items()}}
    log_lines.append(
        f"[JP-1] 候補{len(raw_candidates)}銘柄・保有{len(held_tickers)}銘柄・価格取得{len(market_prices)}銘柄"
    )

    lot_sizes = jp_lotsize.get_lot_sizes()
    company_tickers = jp_lotsize.get_company_tickers()

    # 損切り後の再エントリー制限（2026-10-07追加。SPEC_RSI30.md「2026-10-07改訂」参照）。
    # raw_candidatesの"price"はfrozen候補の価格（prior close）そのものなので分割調整だけすればよい。
    stop_loss_history_all = rsi_strategy.latest_rule_closures(jp_rsi_ledger.read_trade_rows())
    candidate_tickers_today = {c["ticker"] for c in raw_candidates}
    stop_loss_history = {t: sl for t, sl in stop_loss_history_all.items() if t in candidate_tickers_today}
    stop_loss_history = _split_adjusted_stop_loss_history_jp(stop_loss_history, trading_date, log_lines)
    sl_check_candidates = [
        {"ticker": c["ticker"], "price": c["price"]} for c in raw_candidates if c["ticker"] in stop_loss_history
    ]
    _, sl_blocked = rsi_strategy.filter_stop_loss_reentries(sl_check_candidates, stop_loss_history, trading_date)
    sl_blocked_tickers = {b["ticker"] for b in sl_blocked}
    for b in sl_blocked:
        log_lines.append(
            f"[JP-1] 新規エントリー見送り(損切り後の再エントリー制限): {b['ticker']} "
            f"候補価格が閾値{b['threshold']:.2f}(損切り価格{b['stop_loss_price']:.2f}×0.85)を上回り、"
            f"損切り日{b['stop_loss_date']}から{b['trading_days_elapsed']}営業日しか経過していない"
        )

    raw_entry_candidates = [
        c for c in raw_candidates if c["ticker"] in market_prices and c["ticker"] not in sl_blocked_tickers
    ]
    entry_candidates0, blocked_entry_tickers = rsi_strategy.filter_blocked_entries(raw_entry_candidates, state["lots"])
    company_candidates, non_company_tickers = filter_non_company_entries(entry_candidates0, company_tickers)
    entry_candidates, no_lotsize = build_entry_candidates(company_candidates, lot_sizes)
    for ticker in blocked_entry_tickers:
        log_lines.append(f"[JP-1] 新規エントリー見送り(保有中のため): {ticker}")
    for ticker in non_company_tickers:
        log_lines.append(f"[JP-1] {ticker} は会社の株ではないため対象外（REIT等）")
    for ticker in no_lotsize:
        log_lines.append(f"[JP-1] 新規エントリー見送り(lot_size不明): {ticker}")

    # dry-run専用のスワップ売却プレビュー（2026-10-07追加）。do_trade=Falseのため本番実行では
    # 通らない経路（下のdo_trade内で改めて正式に判定・台帳反映する）。ファイル書き込みは行わない。
    if dry_run:
        _, preview_unfunded = rsi_strategy.select_entries_with_unfunded(
            entry_candidates, state["cash_jpy"], rsi_strategy.JP_RULES,
        )
        if preview_unfunded:
            _preview_swaps_jp(state, preview_unfunded, raw_candidates, market_prices, trading_date, log_lines)

    do_trade = not already_processed_today and not dry_run

    if do_trade:
        # --- 1. 損切り・利確（SELL群を先に処理して現金を作る） ---
        for lot in sorted(state["lots"], key=lambda x: (x["ticker"], x["lot_id"])):
            if lot.get("closed"):
                continue
            price = market_prices.get(lot["ticker"])
            if price is None:
                logger.warning("JP: %s の価格が取得できずロット%sの判定をスキップした", lot["ticker"], lot["lot_id"])
                continue
            if not rsi_strategy.is_valid_price(price):
                logger.warning("JP: %s の価格が不正(%r)のためロット%sの判定をスキップした", lot["ticker"], price, lot["lot_id"])
                continue
            idx = next(i for i, x in enumerate(state["lots"]) if x["lot_id"] == lot["lot_id"])

            stop = rsi_strategy.decide_stop_loss(state["lots"][idx], price, rsi_strategy.JP_RULES)
            if stop is not None:
                qty = stop["qty"]
                realized_pnl, realized_pnl_pct = rsi_strategy.compute_realized_pnl(
                    lot["avg_cost"], price, qty,
                )
                state["lots"][idx] = rsi_strategy.apply_stop_loss_fill(state["lots"][idx], qty, trading_date)
                state["cash_jpy"] += qty * price
                trade_row = {
                    "date": trading_date, "action": "SELL", "ticker": stop["ticker"],
                    "shares": qty, "price": round(price, 2), "amount_jpy": round(qty * price, 0),
                    "rule": "stop_loss", "lot_id": stop["lot_id"],
                    "realized_pnl": realized_pnl, "realized_pnl_pct": realized_pnl_pct,
                    "note": "moomoo発注なし・台帳のみの仮想売買",
                    "name": state["lots"][idx].get("name"),
                }
                jp_rsi_ledger.append_trade_row(trade_row)
                accepted_trades.append(trade_row)
                continue  # 損切りした日は利確判定を行わない

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
                realized_pnl, realized_pnl_pct = rsi_strategy.compute_realized_pnl(
                    lot["avg_cost"], price, qty,
                )
                if intent["kind"] == "profit1":
                    state["lots"][idx] = rsi_strategy.apply_profit1_fill(state["lots"][idx], qty, intent["base_shares"])
                else:
                    state["lots"][idx] = rsi_strategy.apply_profit2_fill(state["lots"][idx], qty)
                state["cash_jpy"] += qty * price
                trade_row = {
                    "date": trading_date, "action": "SELL", "ticker": intent["ticker"],
                    "shares": qty, "price": round(price, 2), "amount_jpy": round(qty * price, 0),
                    "rule": intent["kind"], "lot_id": intent["lot_id"],
                    "realized_pnl": realized_pnl, "realized_pnl_pct": realized_pnl_pct,
                    "note": "moomoo発注なし・台帳のみの仮想売買",
                    "name": state["lots"][idx].get("name"),
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
            if not rsi_strategy.is_valid_price(price):
                logger.warning("JP: %s の価格が不正(%r)のためロット%sの判定をスキップした", lot["ticker"], price, lot["lot_id"])
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
                    "name": state["lots"][idx].get("name"),
                }
                jp_rsi_ledger.append_trade_row(trade_row)
                accepted_trades.append(trade_row)

        # --- 3. 新規エントリー（RSIが低い順。現金が足りる分だけ。保有中・利確前は抑止済み） ---
        selected, unfunded_entries = rsi_strategy.select_entries_with_unfunded(
            entry_candidates, state["cash_jpy"], rsi_strategy.JP_RULES,
        )
        for cand in selected:
            lot_id = _new_lot_id_jp(cand["ticker"], trading_date, state["lots"])
            new_lot = rsi_strategy.new_lot(
                cand["ticker"], lot_id, trading_date, cand["qty"], cand["price"], cand["lot_size"],
                name=cand.get("name"),
            )
            state["lots"].append(new_lot)
            cost = cand["qty"] * cand["price"]
            state["cash_jpy"] -= cost
            trade_row = {
                "date": trading_date, "action": "BUY", "ticker": cand["ticker"],
                "shares": cand["qty"], "price": round(cand["price"], 2), "amount_jpy": round(cost, 0),
                "rule": "entry", "lot_id": lot_id,
                "note": f"RSI14={cand['rsi14']:.1f} lot_size={cand['lot_size']}・moomoo発注なし・台帳のみの仮想売買",
                "name": cand.get("name"),
            }
            jp_rsi_ledger.append_trade_row(trade_row)
            accepted_trades.append(trade_row)

        # --- 4. スワップ売却（資金不足の候補を保有ロットの入れ替え売りで拾う。2026-10-07追加。
        #     台帳のみの仮想売買のため、米国枠にある「売りが未達なら買わない」分岐は無い） ---
        if unfunded_entries:
            swap_trades = _run_swaps_jp(state, unfunded_entries, raw_candidates, market_prices, trading_date, log_lines)
            accepted_trades.extend(swap_trades)

        state["last_processed_date"] = trading_date
        log_lines.append(f"[JP-2] 約定{len(accepted_trades)}件（損切り/利確/買い増し/新規エントリー/スワップ込み）")
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
