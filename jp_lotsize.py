"""vs-sp500: 日本株RSI枠の単元株数(lot_size)・会社銘柄一覧キャッシュ（universe.pyの月初更新方式に倣う）。

moomooのget_stock_basicinfo(Market.JP, SecurityType.STOCK)は権限不要で呼べ、
全銘柄のlot_sizeを一括取得できる（2026-08-24実機確認: 3,754銘柄、lot_size内訳は
100が大半・一部REIT等が1・1000も1銘柄・0（無効値）が2銘柄）。毎回全件取りに行かず、
jp_lotsize.jsonにキャッシュし月初のみ更新する（大将「毎回全件取りに行かず、
ファイルにキャッシュして月初のみ更新する」）。

2026-08-27追加: 大将「会社しか買うなと言ってるだろ。reitは除外せよ」を受け、
SecurityType.STOCKに載っている銘柄コードの集合も同じキャッシュに保存する
（実機確認: 3455・2979等のJ-REITはSTOCK区分に無くETF区分でのみ返る。
6367・9983・7532・3905等の会社はSTOCK区分にある）。「会社の株のみ」ルールの判定に使う。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import threading
from typing import Any, Callable

import config

logger = logging.getLogger("vs-sp500.jp_lotsize")

_MOOMOO_TIMEOUT_SEC = 15.0


def _run_with_timeout(fn: Callable[[], Any], timeout: float = _MOOMOO_TIMEOUT_SEC) -> Any | None:
    """broker.pyと同じ方式: デーモンスレッドで実行しtimeoutで見切りをつける。"""
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
        logger.error("lot_size取得の呼び出しがタイムアウトした（%s秒）", timeout)
        return None
    if "value" in error:
        logger.error("lot_size取得の呼び出しが例外を送出した: %s", error["value"])
        return None
    return result.get("value")


def _fetch_from_moomoo() -> dict[str, Any] | None:
    """get_stock_basicinfoで日本株全銘柄のlot_sizeと「会社の株」一覧を取得する。失敗時None。

    stock_type=STOCKに加えETFも取得しマージする（2026-08-24実機確認: J-REIT等
    （例: JP.3455）はmoomoo上でstock_type=STOCKでは返らずETF扱いになっている。
    「REIT等はlot_sizeが100ではない。必ずget_stock_basicinfoの実値を使い、100と決め打ちしない」
    というSPECの警告どおり、STOCKだけを見るとREITのlot_sizeが丸ごと欠測する）。

    lot_size<=0（無効値。実機確認で2銘柄が0だった）の銘柄はlot_sizeキャッシュに含めない
    （「lot_sizeが取得できない銘柄は買わずにログに残す」＝そもそも辞書に無ければ
    呼び出し側が自然に「取得できない」として扱える）。

    stock_tickers（stock_type=STOCKに載っている銘柄コード）はlot_sizeの有効性と無関係に
    そのまま記録する（「会社の株か」の判定であり、lot_sizeの取得可否とは別問題のため）。

    戻り値: {"lot_sizes": {ticker: lot_size}, "stock_tickers": [ticker, ...]}
    """

    def _call() -> dict[str, Any]:
        from moomoo import Market, OpenQuoteContext, SecurityType

        ctx = OpenQuoteContext(host=config.MOOMOO_HOST, port=config.MOOMOO_PORT)
        try:
            lot_sizes: dict[str, int] = {}
            stock_tickers: list[str] = []
            for stock_type in (SecurityType.STOCK, SecurityType.ETF):
                ret, data = ctx.get_stock_basicinfo(market=Market.JP, stock_type=stock_type)
                if ret != 0:
                    raise RuntimeError(f"get_stock_basicinfo({stock_type})失敗: {data}")
                for row in data.to_dict(orient="records"):
                    code = str(row["code"])  # 例: 'JP.6367'
                    ticker = code[3:] if code.startswith("JP.") else code
                    if stock_type == SecurityType.STOCK:
                        stock_tickers.append(ticker)
                    lot_size = int(row["lot_size"])
                    if lot_size <= 0:
                        continue
                    lot_sizes[ticker] = lot_size
            return {"lot_sizes": lot_sizes, "stock_tickers": stock_tickers}
        finally:
            ctx.close()

    return _run_with_timeout(_call)


def _load_cache() -> dict[str, Any] | None:
    if not config.RSI_JP_LOTSIZE_CACHE_PATH.exists():
        return None
    try:
        with open(config.RSI_JP_LOTSIZE_CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("jp_lotsize.json読み込み失敗: %s", e)
        return None


def _save_cache(data: dict[str, Any]) -> None:
    config.RSI_JP_LOTSIZE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = config.RSI_JP_LOTSIZE_CACHE_PATH.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp_path.replace(config.RSI_JP_LOTSIZE_CACHE_PATH)


def _needs_refresh(cache: dict[str, Any] | None, today: dt.date) -> bool:
    # "stock_tickers"は2026-08-27追加のため、旧キャッシュ（それ以前に取得済み）には無い。
    # 欠けている場合は月が同じでも再取得する（スキーマ移行を自然に1回だけ行うため）。
    if cache is None or not cache.get("lot_sizes") or "stock_tickers" not in cache:
        return True
    try:
        fetched_date = dt.date.fromisoformat(cache["fetched_date"])
    except (KeyError, ValueError):
        return True
    return (fetched_date.year, fetched_date.month) != (today.year, today.month)


def _get_or_refresh_cache(today: dt.date, force_refresh: bool) -> dict[str, Any]:
    """lot_sizes・stock_tickers共通のキャッシュ取得（月初のみ再取得）。

    get_lot_sizesとget_company_tickersの両方から呼ばれる。同一日に両方呼ばれても
    ディスクキャッシュが更新済みになるため、再取得(moomoo呼び出し)は最大1回で済む。
    """
    cache = _load_cache()
    if not force_refresh and not _needs_refresh(cache, today):
        return cache

    fetched = _fetch_from_moomoo()
    if fetched is None:
        if cache is not None and cache.get("lot_sizes"):
            logger.warning("jp_lotsize再取得に失敗したため、古いキャッシュ(%s取得)をそのまま使う", cache.get("fetched_date"))
            return cache
        logger.error("jp_lotsize取得に失敗し、キャッシュも無いため空データを返す")
        return {"lot_sizes": {}, "stock_tickers": []}

    data = {
        "lot_sizes": fetched["lot_sizes"],
        "stock_tickers": fetched["stock_tickers"],
        "source": "moomoo get_stock_basicinfo(Market.JP)",
        "fetched_date": today.isoformat(),
    }
    _save_cache(data)
    logger.info(
        "jp_lotsize更新完了: %d銘柄（うち会社の株%d銘柄）", len(fetched["lot_sizes"]), len(fetched["stock_tickers"]),
    )
    return data


def get_lot_sizes(today: dt.date | None = None, force_refresh: bool = False) -> dict[str, int]:
    """{"6367": 100, "268A": 100, ...} を返す（日本株ティッカー→単元株数）。

    月初のみ再取得し、それ以外はjp_lotsize.jsonのキャッシュをそのまま返す。
    取得に失敗し、かつキャッシュも無い場合は空辞書を返す（呼び出し側は
    「lot_sizeが取得できない銘柄は買わずにログに残す」の方針で自然に全銘柄をスキップする）。
    """
    today = today or dt.date.today()
    return dict(_get_or_refresh_cache(today, force_refresh).get("lot_sizes", {}))


def get_company_tickers(today: dt.date | None = None, force_refresh: bool = False) -> set[str]:
    """moomoo get_stock_basicinfo(SecurityType.STOCK)に載っている銘柄コードの集合を返す。

    「会社の株のみを買う」ルール（2026-08-27・大将「reitは除外せよ」）の判定に使う。
    J-REIT等はSTOCK区分に載らない（ETF区分でのみ返る）ため、ここに含まれない。
    lot_sizesと同じキャッシュ・同じ月初更新規約を共有する。
    """
    today = today or dt.date.today()
    return set(_get_or_refresh_cache(today, force_refresh).get("stock_tickers", []))
