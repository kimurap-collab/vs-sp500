"""vs-sp500: moomoo APIの薄いラッパ。外部依存(moomoo SDK)をここに閉じ込める。

moomoo SDKは無応答で固まる実績があるため、全呼び出しをワーカースレッド上で実行し
timeout付きjoinで見切りをつける（signal.alarmでは止められないことを実測で確認済み。
2026-08-14: signal.alarm(8)を設定してもaccinfo_queryが120秒超ブロックし続けた）。
タイムアウト時、ワーカースレッドはdaemon=Trueのため呼び出し元をブロックし続けず、
プロセス終了時に道連れで破棄される。
"""
from __future__ import annotations

import datetime as dt
import logging
import socket
import threading
import time
from typing import Any, Callable

import config

logger = logging.getLogger("vs-sp500.broker")

CONNECT_CHECK_TIMEOUT_SEC = 3.0
CALL_TIMEOUT_SEC = 15.0
ORDER_FILL_TIMEOUT_SEC = 30.0
ORDER_POLL_INTERVAL_SEC = 1.0

# これ以上約定が進まないと確定できる終端ステータス（FILLED_ALLは別扱いの成功終端）。
# CANCELLED_PARTは「一部約定・残数はキャンセル」を意味するためここに含める。
ORDER_TERMINAL_STATUSES = (
    "FAILED", "SUBMIT_FAILED", "CANCELLED_ALL", "CANCELLED_PART",
    "DISABLED", "DELETED", "FILL_CANCELLED", "TIMEOUT",
)


def ticker_to_code(ticker: str) -> str:
    """台帳ティッカー（例: 'BRK-B'）をmoomooのcode（例: 'US.BRK.B'）に変換する。"""
    return f"US.{ticker.replace('-', '.')}"


def code_to_ticker(code: str) -> str:
    """moomooのcode（例: 'US.BRK.B'）を台帳ティッカー（例: 'BRK-B'）に変換する。"""
    raw = code[3:] if code.startswith("US.") else code
    return raw.replace(".", "-")


def is_available() -> bool:
    """OpenD (127.0.0.1:11111) にTCPで到達できるか確認する。

    moomoo SDK自体は接続失敗時に無限リトライする実績があるため、SDKを呼ぶ前に
    ここで生死を高速判定する（これが呼び出し元にとって唯一の高速な縮退判定手段）。
    """
    try:
        with socket.create_connection(
            (config.MOOMOO_HOST, config.MOOMOO_PORT), timeout=CONNECT_CHECK_TIMEOUT_SEC
        ):
            return True
    except OSError as e:
        logger.warning("OpenD未接続: %s", e)
        return False


def _run_with_timeout(fn: Callable[[], Any], timeout: float = CALL_TIMEOUT_SEC) -> Any | None:
    """関数をデーモンスレッドで実行し、timeout秒でjoinを諦めて呼び出し元に制御を返す。

    タイムアウトした場合、スレッド自体は残存する可能性がある（moomoo SDKが本当に
    無応答なケース）が、daemon=Trueなのでプロセス終了は妨げない。
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


def _open_trade_ctx():
    from moomoo import OpenSecTradeContext, SecurityFirm, TrdMarket
    return OpenSecTradeContext(
        filter_trdmarket=TrdMarket.US,
        host=config.MOOMOO_HOST,
        port=config.MOOMOO_PORT,
        security_firm=SecurityFirm.FUTUSG,
    )


def get_positions() -> dict[str, float] | None:
    """{'VOO': 31.0, ...} を返す。失敗時None。"""

    def _call() -> dict[str, float]:
        from moomoo import TrdEnv

        ctx = _open_trade_ctx()
        try:
            ret, data = ctx.position_list_query(trd_env=TrdEnv.SIMULATE, acc_id=config.MOOMOO_ACC_ID)
            if ret != 0:
                raise RuntimeError(f"position_list_query失敗: {data}")
            positions: dict[str, float] = {}
            for row in data.to_dict(orient="records"):
                ticker = code_to_ticker(str(row["code"]))  # 例: 'US.BRK.B' → 'BRK-B'
                positions[ticker] = float(row["qty"])
            return positions
        finally:
            ctx.close()

    return _run_with_timeout(_call)


def get_cash() -> float | None:
    """口座の現金(USD)。失敗時None。"""

    def _call() -> float:
        from moomoo import TrdEnv

        ctx = _open_trade_ctx()
        try:
            ret, data = ctx.accinfo_query(trd_env=TrdEnv.SIMULATE, acc_id=config.MOOMOO_ACC_ID, currency="USD")
            if ret != 0:
                raise RuntimeError(f"accinfo_query失敗: {data}")
            return float(data.iloc[0]["cash"])
        finally:
            ctx.close()

    return _run_with_timeout(_call)


def get_snapshot(tickers: list[str]) -> dict[str, float] | None:
    """現在値（照合用）。{'VOO': 714.95, ...}。失敗時None。"""

    def _call() -> dict[str, float]:
        from moomoo import OpenQuoteContext

        ctx = OpenQuoteContext(host=config.MOOMOO_HOST, port=config.MOOMOO_PORT)
        try:
            codes = [ticker_to_code(t) for t in tickers]
            ret, data = ctx.get_market_snapshot(codes)
            if ret != 0:
                raise RuntimeError(f"get_market_snapshot失敗: {data}")
            result: dict[str, float] = {}
            for row in data.to_dict(orient="records"):
                ticker = code_to_ticker(str(row["code"]))
                result[ticker] = float(row["last_price"])
            return result
        finally:
            ctx.close()

    return _run_with_timeout(_call)


def _split_ratio_from_rehab_row(row: dict[str, Any]) -> float | None:
    """get_rehabの1行から株数の倍率（分割後の株数/分割前の株数）を返す。分割・併合の行でなければNone。

    実機確認（2026-10-01）: 分割は split_base→split_ert（NVDA 2024-06-10: 1→10）、
    併合は join_base→join_ert（GE 2021-08-02: 8→1）。配当だけの行はどちらもNaN。
    """
    for base_key, ert_key in (("split_base", "split_ert"), ("join_base", "join_ert")):
        base, ert = row.get(base_key), row.get(ert_key)
        if base is None or ert is None or base != base or ert != ert:  # NaN判定
            continue
        if float(base) > 0 and float(ert) > 0:
            return float(ert) / float(base)
    return None


def get_splits(tickers: list[str]) -> dict[str, list[tuple[str, float]]] | None:
    """moomooのget_rehab（復権情報）から株式分割・併合の履歴を取得する（2026-10-01追加）。

    戻り値: {ticker: [(分割日 "YYYY-MM-DD", ratio), ...]}。ratioは分割後の株数/分割前の株数。
    接続失敗・タイムアウトはNone。個別銘柄の取得失敗はその銘柄を戻り値に含めない（呼び出し側でWARNING）。
    """
    if not tickers:
        return {}

    def _call() -> dict[str, list[tuple[str, float]]]:
        from moomoo import OpenQuoteContext

        ctx = OpenQuoteContext(host=config.MOOMOO_HOST, port=config.MOOMOO_PORT)
        try:
            result: dict[str, list[tuple[str, float]]] = {}
            for ticker in tickers:
                ret, data = ctx.get_rehab(ticker_to_code(ticker))
                if ret != 0:
                    logger.warning("get_rehab失敗: %s %s", ticker, data)
                    continue
                splits: list[tuple[str, float]] = []
                for row in data.to_dict(orient="records"):
                    ratio = _split_ratio_from_rehab_row(row)
                    if ratio is not None:
                        splits.append((str(row["ex_div_date"])[:10], ratio))
                result[ticker] = splits
            return result
        finally:
            ctx.close()

    return _run_with_timeout(_call)


def _cash_dividend_from_rehab_row(row: dict[str, Any]) -> float:
    """get_rehabの1行から1株あたりの現金配当額を返す（per_cash_div + special_dividend。
    分割・併合だけの行や配当の無い行は0になる。NaN判定はNaN!=NaNを使う）。
    """
    total = 0.0
    for key in ("per_cash_div", "special_dividend"):
        value = row.get(key)
        if value is None or value != value:  # NaN判定
            continue
        total += float(value)
    return total


def get_dividends(tickers: list[str]) -> dict[str, list[tuple[str, float]]] | None:
    """moomooのget_rehab（復権情報）から1株あたりの現金配当履歴を取得する（2026-10-07追加・Change3）。

    get_splitsと同じctx・タイムアウトラッパーを使う（charter v1.6「基本データ…moomooにしなさいよ」）。
    戻り値: {ticker: [(ex_div_date "YYYY-MM-DD", per_share_jpy_or_usd), ...]}（配当が無い行・0以下は含めない）。
    接続失敗・タイムアウトはNone。個別銘柄の取得失敗はその銘柄を戻り値に含めない（呼び出し側でWARNING）。
    """
    if not tickers:
        return {}

    def _call() -> dict[str, list[tuple[str, float]]]:
        from moomoo import OpenQuoteContext

        ctx = OpenQuoteContext(host=config.MOOMOO_HOST, port=config.MOOMOO_PORT)
        try:
            result: dict[str, list[tuple[str, float]]] = {}
            for ticker in tickers:
                ret, data = ctx.get_rehab(ticker_to_code(ticker))
                if ret != 0:
                    logger.warning("get_rehab失敗(dividend): %s %s", ticker, data)
                    continue
                dividends: list[tuple[str, float]] = []
                for row in data.to_dict(orient="records"):
                    amount = _cash_dividend_from_rehab_row(row)
                    if amount > 0:
                        dividends.append((str(row["ex_div_date"])[:10], amount))
                result[ticker] = dividends
            return result
        finally:
            ctx.close()

    return _run_with_timeout(_call)


_OWNER_PLATE_MIN_CALL_INTERVAL_SEC = 3.5  # moomoo実機確認(2026-10-07): 本APIは最大10回/30秒の
# レート制限がある（"Get Stock's Sector request failed due to high frequency. Maximum 10 times
# per 30 seconds."）。30/10=3.0秒に少し余裕を持たせた間隔を呼び出し間に必ず空ける。
_OWNER_PLATE_CHUNK_SIZE = 10  # 1回あたりの件数（REIT等「ETF type」扱いの銘柄が均等に紛れており、
# config.RSI_SWAP_MOOMOO_BATCH_SIZE(200)では実機確認(2026-10-07)でほぼ確実に1件は混入してバッチ
# 全体がret!=0になるため、この専用の小さいチャンクサイズで呼ぶ）。
_OWNER_PLATE_RATE_LIMIT_RETRY_WAIT_SEC = 31.0  # レート制限に当たった場合、ウィンドウが流れるまで待つ


def get_owner_plates(tickers: list[str]) -> dict[str, list[str]] | None:
    """moomooのget_owner_plateから各銘柄のINDUSTRY分類名リストを取得する（2026-10-07追加。
    スワップ売却のセクター分類に使う。sector_map.classify_industry_labelsへそのまま渡す値）。

    CONCEPT/OTHER分類は使わずINDUSTRYのみ採用する。本APIは最大10回/30秒のレート制限がある
    （実機確認: "Get Stock's Sector request failed due to high frequency."）ため全呼び出しの
    間隔を3.5秒空ける。さらにREIT銘柄の一部（AMT・PLD・O等）がmoomoo側で"ETF type"として
    分類され、1銘柄でも混じるとそのリクエスト全体がret!=0になる実績があり（実機確認: "Get
    Stock's Sector interface does not support ETFs type."）、この手の銘柄は普遍的に分布している
    ためconfig.RSI_SWAP_MOOMOO_BATCH_SIZE(200)の大きなバッチでは毎回失敗する。そのため
    _OWNER_PLATE_CHUNK_SIZE件ずつの小さいチャンクで直接呼ぶ（二分探索での原因銘柄特定は
    呼び出し回数がレート制限と掛け合わさり非現実的な時間になるため行わない）。
    失敗したチャンクは丸ごと諦める（=その銘柄群はセクター不明→Tier0として扱われる。
    「Unknown sector → 0」仕様どおりの劣化。REIT等は本来XLREだが判定できず中立0になる）。
    518銘柄規模で数分かかる想定だが月1回しか呼ばないため許容する。
    """
    if not tickers:
        return {}

    def _call() -> dict[str, list[str]]:
        from moomoo import OpenQuoteContext

        ctx = OpenQuoteContext(host=config.MOOMOO_HOST, port=config.MOOMOO_PORT)
        try:
            result: dict[str, list[str]] = {t: [] for t in tickers}
            last_call_at: list[float] = [0.0]

            def _call_owner_plate(chunk: list[str]):
                wait = _OWNER_PLATE_MIN_CALL_INTERVAL_SEC - (time.monotonic() - last_call_at[0])
                if wait > 0:
                    time.sleep(wait)
                last_call_at[0] = time.monotonic()
                ret, data = ctx.get_owner_plate([ticker_to_code(t) for t in chunk])
                if ret == 0 or "frequency" not in str(data).lower():
                    return ret, data
                logger.warning(
                    "get_owner_plates: レート制限のため%.0f秒待って再試行", _OWNER_PLATE_RATE_LIMIT_RETRY_WAIT_SEC,
                )
                time.sleep(_OWNER_PLATE_RATE_LIMIT_RETRY_WAIT_SEC)
                last_call_at[0] = time.monotonic()
                return ctx.get_owner_plate([ticker_to_code(t) for t in chunk])

            for i in range(0, len(tickers), _OWNER_PLATE_CHUNK_SIZE):
                chunk = tickers[i:i + _OWNER_PLATE_CHUNK_SIZE]
                ret, data = _call_owner_plate(chunk)
                if ret != 0:
                    logger.warning("get_owner_plates: %d件を分類できず除外する: %s", len(chunk), data)
                    continue
                for row in data.to_dict(orient="records"):
                    if str(row.get("plate_type")) != "INDUSTRY":
                        continue
                    ticker = code_to_ticker(str(row["code"]))
                    if ticker in result:
                        result[ticker].append(str(row["plate_name"]))
            return result
        finally:
            ctx.close()

    return _run_with_timeout(_call, timeout=300.0)


def get_sector_etf_returns(etfs: tuple[str, ...], lookback_trading_days: int) -> dict[str, float] | None:
    """SPDRセクターETFの直近lookback_trading_days営業日リターン(%刻みではなく比率)を
    request_history_klineで取得する（2026-10-07追加。月初回のみ呼ぶ想定＝kline取得枠は
    僅かしか消費しない）。個別ETFの取得失敗はそのETFを戻り値から省く（呼び出し側がWARNING）。
    接続失敗・タイムアウトはNone。
    """
    def _call() -> dict[str, float]:
        from moomoo import AuType, KLType, OpenQuoteContext

        ctx = OpenQuoteContext(host=config.MOOMOO_HOST, port=config.MOOMOO_PORT)
        try:
            # 土日・休場を見込んだ余裕を持たせてから末尾lookback_trading_days+1本だけ使う
            lookback_days_calendar = int(lookback_trading_days * 1.6) + 10
            start = (dt.date.today() - dt.timedelta(days=lookback_days_calendar)).isoformat()
            end = dt.date.today().isoformat()
            result: dict[str, float] = {}
            for etf in etfs:
                ret, data, _ = ctx.request_history_kline(
                    ticker_to_code(etf), start=start, end=end, ktype=KLType.K_DAY, autype=AuType.QFQ,
                )
                if ret != 0:
                    logger.warning("get_sector_etf_returns: %s のkline取得失敗: %s", etf, data)
                    continue
                closes = data["close"].astype(float).tolist()
                if len(closes) <= lookback_trading_days:
                    logger.warning("get_sector_etf_returns: %s の本数不足(%d本)", etf, len(closes))
                    continue
                result[etf] = closes[-1] / closes[-1 - lookback_trading_days] - 1.0
            return result
        finally:
            ctx.close()

    return _run_with_timeout(_call, timeout=CALL_TIMEOUT_SEC * 3)


def get_market_caps(tickers: list[str]) -> dict[str, float] | None:
    """get_market_snapshotのtotal_market_val（時価総額）を全銘柄分取得する（2026-10-07追加。
    毎晩実行する想定。日足取得枠(kline quota)は消費しない）。
    config.RSI_SWAP_MOOMOO_BATCH_SIZE件ずつバッチで呼ぶ。接続失敗・タイムアウトはNone。
    """
    if not tickers:
        return {}

    def _call() -> dict[str, float]:
        from moomoo import OpenQuoteContext

        ctx = OpenQuoteContext(host=config.MOOMOO_HOST, port=config.MOOMOO_PORT)
        try:
            result: dict[str, float] = {}

            def _fetch_chunk(chunk: list[str]) -> None:
                # 1銘柄でもmoomooが認識できない(例: 上場廃止・改称直後で未反映)だとバッチ全体が
                # ret!=0になる実績があるため（2026-10-07実機確認: "Unknown stock. PSKY"）、
                # 失敗したバッチは半分に割って再試行し、原因銘柄だけを特定して除外する。
                codes = [ticker_to_code(t) for t in chunk]
                ret, data = ctx.get_market_snapshot(codes)
                if ret == 0:
                    for row in data.to_dict(orient="records"):
                        ticker = code_to_ticker(str(row["code"]))
                        cap = row.get("total_market_val")
                        if cap is not None and cap == cap and float(cap) > 0:  # cap==capはNaN除外
                            result[ticker] = float(cap)
                    return
                if len(chunk) == 1:
                    logger.warning("get_market_caps: %s の時価総額を取得できず除外する: %s", chunk[0], data)
                    return
                mid = len(chunk) // 2
                _fetch_chunk(chunk[:mid])
                _fetch_chunk(chunk[mid:])

            batch = config.RSI_SWAP_MOOMOO_BATCH_SIZE
            for i in range(0, len(tickers), batch):
                _fetch_chunk(tickers[i:i + batch])
            return result
        finally:
            ctx.close()

    return _run_with_timeout(_call, timeout=CALL_TIMEOUT_SEC * 3)


def get_history_kl_quota() -> tuple[int, int] | None:
    """moomooの日足取得枠(kline quota)の使用量を返す (used, remain)（2026-10-07追加。
    スワップ機能のセクターTier計算が消費するkline枠を報告するための運用確認用）。失敗時None。
    """
    def _call() -> tuple[int, int]:
        from moomoo import OpenQuoteContext

        ctx = OpenQuoteContext(host=config.MOOMOO_HOST, port=config.MOOMOO_PORT)
        try:
            ret, data = ctx.get_history_kl_quota(get_detail=False)
            if ret != 0:
                raise RuntimeError(f"get_history_kl_quota失敗: {data}")
            used, remain = data[0], data[1]
            return int(used), int(remain)
        finally:
            ctx.close()

    return _run_with_timeout(_call)


def place_market_order(ticker: str, qty: int, side: str) -> dict[str, Any] | None:
    """成行注文を出し、可能なら約定まで待つ（最大ORDER_FILL_TIMEOUT_SEC秒）。

    戻り値: {'order_id': str, 'status': str, 'filled_qty': int, 'avg_price': float}。
    未約定・一部約定のまま待ち時間が尽きた場合もNoneにはせず、その時点の状態を返す
    （呼び出し元がclassify_fillで分類し、未約定分をpending_ordersとして追跡する）。
    注文の送信そのものが失敗した場合のみNone。
    """
    if side not in ("BUY", "SELL"):
        raise ValueError(f"不正なside: {side}")
    if qty <= 0:
        raise ValueError(f"不正なqty: {qty}")

    def _call() -> dict[str, Any]:
        from moomoo import OrderType, TrdEnv, TrdSide

        ctx = _open_trade_ctx()
        try:
            trd_side = TrdSide.BUY if side == "BUY" else TrdSide.SELL
            # fill_outside_rth は渡さない。moomooサーバは MARKET 注文にこのフラグが付いとると
            # 場中であっても "Can only place RTH market orders" で提出を拒否する
            # （2026-08-17 23:04 実測＝11:04 EDT・RTH内。同じ瞬間にフラグ有りは拒否、
            #   フラグ無しは検証を通過して株数上限で拒否＝原因は時刻やなくフラグやと確定）。
            #
            # 経緯: このフラグは2026-08-14に「毎朝7時JST＝RTH終了後に実行されるので、RTH限定だと
            # 注文がSUBMITTEDのまま張り付く」という理由で追加された。しかしMARKET注文では
            # そもそも設定できず、追加以降の全注文が提出時点で拒否され続けとった（4日間、
            # 実注文が発生せんかったので気付けんかった）。
            # 現在は実行が23:00ローカル（=11:00 EDT）に移りRTH内なので、フラグ自体が不要や。
            ret, data = ctx.place_order(
                price=0,
                qty=qty,
                code=ticker_to_code(ticker),
                trd_side=trd_side,
                order_type=OrderType.MARKET,
                trd_env=TrdEnv.SIMULATE,
                acc_id=config.MOOMOO_ACC_ID,
            )
            if ret != 0:
                raise RuntimeError(f"place_order失敗: {data}")
            order_id = str(data.iloc[0]["order_id"])

            status = "SUBMITTED"
            filled_qty = 0
            avg_price = 0.0
            deadline = time.monotonic() + ORDER_FILL_TIMEOUT_SEC
            while time.monotonic() < deadline:
                ret, orders = ctx.order_list_query(
                    order_id=order_id, trd_env=TrdEnv.SIMULATE, acc_id=config.MOOMOO_ACC_ID
                )
                if ret == 0 and not orders.empty:
                    row = orders.iloc[0]
                    status = str(row["order_status"])
                    filled_qty = int(float(row["dealt_qty"]))
                    avg_price = float(row["dealt_avg_price"])
                    if status == "FILLED_ALL" or status in ORDER_TERMINAL_STATUSES:
                        break
                time.sleep(ORDER_POLL_INTERVAL_SEC)
            # タイムアウトで抜けた場合、statusはまだSUBMITTED等の未確定のまま。
            # 未約定・一部約定分は呼び出し元がpending_ordersとして追跡し、次回実行の冒頭で決済する。
            return {"order_id": order_id, "status": status, "filled_qty": filled_qty, "avg_price": avg_price}
        finally:
            ctx.close()

    return _run_with_timeout(_call, timeout=ORDER_FILL_TIMEOUT_SEC + 10)


def classify_fill(requested_qty: int, fill: dict[str, Any]) -> str:
    """place_market_order/get_order_statusの戻り値を、要求株数と突き合わせて分類する。

    'FULL': 全量約定。
    'PARTIAL_OPEN': 一部約定・注文は継続中（残りは追跡が必要）。
    'PARTIAL_TERMINAL': 一部約定のまま終端（残りは打ち切り、これ以上約定しない）。
    'NONE_OPEN': 未約定・注文は継続中（全量を追跡する）。
    'NONE_TERMINAL': 未約定のまま終端（実質的な発注失敗）。
    """
    filled_qty = fill["filled_qty"]
    is_terminal = fill["status"] in ORDER_TERMINAL_STATUSES
    if filled_qty >= requested_qty:
        return "FULL"
    if filled_qty > 0:
        return "PARTIAL_TERMINAL" if is_terminal else "PARTIAL_OPEN"
    return "NONE_TERMINAL" if is_terminal else "NONE_OPEN"


def get_order_status(order_id: str) -> dict[str, Any] | None:
    """指定order_idの現在状態をmoomooに問い合わせる（未決注文の決済に使う）。

    まずorder_list_query（アクティブ注文）を試し、見つからなければ
    history_order_list_query（確定済み注文。過去90日分）を試す。
    戻り値: {'order_id': str, 'status': str, 'filled_qty': int, 'avg_price': float,
             'updated_date': str | None}。
    アクティブ注文にも過去90日の履歴にも見つからない場合は status='NOT_FOUND' の辞書を返す
    （問い合わせ自体は成功しているため、これは呼び出し自体の失敗とは区別する。
    呼び出し元はpending_orderを解決済みとして扱ってよい＝2026-08-18 修正2）。
    問い合わせ自体が失敗した場合（タイムアウト・例外）はNoneを返す
    （呼び出し元はpending_orderをそのまま残すこと＝黙って消さない）。
    """

    def _call() -> dict[str, Any]:
        from moomoo import TrdEnv

        ctx = _open_trade_ctx()
        try:
            ret, data = ctx.order_list_query(
                order_id=order_id, trd_env=TrdEnv.SIMULATE, acc_id=config.MOOMOO_ACC_ID
            )
            if ret != 0:
                raise RuntimeError(f"order_list_query失敗: {data}")
            if data.empty:
                ret2, data2 = ctx.history_order_list_query(trd_env=TrdEnv.SIMULATE, acc_id=config.MOOMOO_ACC_ID)
                if ret2 != 0:
                    raise RuntimeError(f"history_order_list_query失敗: {data2}")
                data2 = data2[data2["order_id"].astype(str) == str(order_id)]
                if data2.empty:
                    return {
                        "order_id": str(order_id), "status": "NOT_FOUND",
                        "filled_qty": 0, "avg_price": 0.0, "updated_date": None,
                    }
                row = data2.iloc[0]
            else:
                row = data.iloc[0]
            updated = str(row.get("updated_time") or "")
            return {
                "order_id": str(order_id),
                "status": str(row["order_status"]),
                "filled_qty": int(float(row["dealt_qty"])),
                "avg_price": float(row["dealt_avg_price"]),
                "updated_date": updated.split(" ")[0] if updated else None,
            }
        finally:
            ctx.close()

    return _run_with_timeout(_call, timeout=CALL_TIMEOUT_SEC)


def cancel_order(order_id: str) -> bool:
    """指定order_idの注文をキャンセルする（滞留注文の自己解決に使う。2026-08-18 修正2）。

    modify_order(ModifyOrderOp.CANCEL, order_id, qty, price, ...) が正しい呼び方
    （2026-08-18 実機確認: 存在しないorder_idで呼び出したところ
    ret=-1, msg='This order ID does not exist.' が返り、例外にならず正常に呼べることを確認した）。
    成功時True。失敗時（moomoo側の拒否・タイムアウト・例外）はFalse。
    """

    def _call() -> bool:
        from moomoo import ModifyOrderOp, TrdEnv

        ctx = _open_trade_ctx()
        try:
            ret, data = ctx.modify_order(
                ModifyOrderOp.CANCEL, order_id, 0, 0,
                trd_env=TrdEnv.SIMULATE, acc_id=config.MOOMOO_ACC_ID,
            )
            if ret != 0:
                raise RuntimeError(f"modify_order(CANCEL)失敗: {data}")
            return True
        finally:
            ctx.close()

    result = _run_with_timeout(_call, timeout=CALL_TIMEOUT_SEC)
    return bool(result)


# 米国市場が「場中」であることを示す market_us の値。
# 公式ドキュメント（https://openapi.moomoo.com/moomoo-api-doc/en/quote/quote.html）で
# QotMarketState_Afternoon (5) = "Afternoon session / Regular trading hours for U.S stock market"
# と明記されている通り、米国株の通常取引時間（RTH）は丸ごと"AFTERNOON"として返る
# （"MORNING"はアジア市場の前場専用で米国では返らない）。
# PRE_MARKET_BEGIN/AFTER_HOURS_BEGIN は時間外取引を示す値であり、AFTERNOONのみを
# 条件にすることで自動的に通常取引時間だけに限定される。
# 2026-08-18 11:55 JST（閉場中）に実機確認したところ market_us='AFTER_HOURS_END' だった。
# 場中の実測値（AFTERNOONが実際に返ること）は未確認のため、初回の場中実行時にログで裏取りすること。
MARKET_US_OPEN_STATE = "AFTERNOON"


def get_global_state() -> dict[str, Any] | None:
    """moomooの全体状態（各市場の開閉状態を含む）を取得する。失敗時None。"""

    def _call() -> dict[str, Any]:
        from moomoo import OpenQuoteContext

        ctx = OpenQuoteContext(host=config.MOOMOO_HOST, port=config.MOOMOO_PORT)
        try:
            ret, data = ctx.get_global_state()
            if ret != 0:
                raise RuntimeError(f"get_global_state失敗: {data}")
            return dict(data)
        finally:
            ctx.close()

    return _run_with_timeout(_call)


def get_market_us_state() -> str | None:
    """moomooから返る米国市場の生の状態文字列（例: 'AFTERNOON'）を取得する。失敗時None。

    新規発注前のゲート判定（2026-08-18 修正2）に使う。呼び出し元は1回の実行につき1回だけ
    呼び、結果を使い回すこと（発注ごとに毎回問い合わせない）。AFTERNOONが本当に返ってくるかを
    初回の本番実行で確認したいため、取得できた値は毎回ログに残す。
    """
    state = get_global_state()
    if state is None:
        return None
    market_us = state.get("market_us")
    logger.info("moomoo market_us=%s", market_us)
    return market_us


def is_market_open_us() -> bool | None:
    """米国市場が場中かを判定する（自前の時差計算はせず、moomooに問い合わせる）。

    戻り値: True=場中 / False=場中でない / None=問い合わせ失敗（呼び出し元は保守的に、
    「場中でない」と同じ扱い＝何もしない、にすること）。
    """
    market_us = get_market_us_state()
    if market_us is None:
        return None
    return market_us == MARKET_US_OPEN_STATE
