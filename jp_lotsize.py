"""vs-sp500: 日本株RSI枠の単元株数(lot_size)キャッシュ（universe.pyの月初更新方式に倣う）。

moomooのget_stock_basicinfo(Market.JP, SecurityType.STOCK)は権限不要で呼べ、
全銘柄のlot_sizeを一括取得できる（2026-08-24実機確認: 3,754銘柄、lot_size内訳は
100が大半・一部REIT等が1・1000も1銘柄・0（無効値）が2銘柄）。毎回全件取りに行かず、
jp_lotsize.jsonにキャッシュし月初のみ更新する（大将「毎回全件取りに行かず、
ファイルにキャッシュして月初のみ更新する」）。
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


def _fetch_from_moomoo() -> dict[str, int] | None:
    """get_stock_basicinfoで日本株全銘柄のlot_sizeを取得する。失敗時None。

    stock_type=STOCKに加えETFも取得しマージする（2026-08-24実機確認: J-REIT等
    （例: JP.3455）はmoomoo上でstock_type=STOCKでは返らずETF扱いになっている。
    「REIT等はlot_sizeが100ではない。必ずget_stock_basicinfoの実値を使い、100と決め打ちしない」
    というSPECの警告どおり、STOCKだけを見るとREITのlot_sizeが丸ごと欠測する）。

    lot_size<=0（無効値。実機確認で2銘柄が0だった）の銘柄はキャッシュに含めない
    （「lot_sizeが取得できない銘柄は買わずにログに残す」＝そもそも辞書に無ければ
    呼び出し側が自然に「取得できない」として扱える）。
    """

    def _call() -> dict[str, int]:
        from moomoo import Market, OpenQuoteContext, SecurityType

        ctx = OpenQuoteContext(host=config.MOOMOO_HOST, port=config.MOOMOO_PORT)
        try:
            result: dict[str, int] = {}
            for stock_type in (SecurityType.STOCK, SecurityType.ETF):
                ret, data = ctx.get_stock_basicinfo(market=Market.JP, stock_type=stock_type)
                if ret != 0:
                    raise RuntimeError(f"get_stock_basicinfo({stock_type})失敗: {data}")
                for row in data.to_dict(orient="records"):
                    lot_size = int(row["lot_size"])
                    if lot_size <= 0:
                        continue
                    code = str(row["code"])  # 例: 'JP.6367'
                    ticker = code[3:] if code.startswith("JP.") else code
                    result[ticker] = lot_size
            return result
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
    if cache is None or not cache.get("lot_sizes"):
        return True
    try:
        fetched_date = dt.date.fromisoformat(cache["fetched_date"])
    except (KeyError, ValueError):
        return True
    return (fetched_date.year, fetched_date.month) != (today.year, today.month)


def get_lot_sizes(today: dt.date | None = None, force_refresh: bool = False) -> dict[str, int]:
    """{"6367": 100, "268A": 100, ...} を返す（日本株ティッカー→単元株数）。

    月初のみ再取得し、それ以外はjp_lotsize.jsonのキャッシュをそのまま返す。
    取得に失敗し、かつキャッシュも無い場合は空辞書を返す（呼び出し側は
    「lot_sizeが取得できない銘柄は買わずにログに残す」の方針で自然に全銘柄をスキップする）。
    """
    today = today or dt.date.today()
    cache = _load_cache()
    if not force_refresh and not _needs_refresh(cache, today):
        return dict(cache["lot_sizes"])

    lot_sizes = _fetch_from_moomoo()
    if lot_sizes is None:
        if cache is not None and cache.get("lot_sizes"):
            logger.warning("lot_size再取得に失敗したため、古いキャッシュ(%s取得)をそのまま使う", cache.get("fetched_date"))
            return dict(cache["lot_sizes"])
        logger.error("lot_size取得に失敗し、キャッシュも無いため空辞書を返す")
        return {}

    data = {
        "lot_sizes": lot_sizes,
        "source": "moomoo get_stock_basicinfo(Market.JP)",
        "fetched_date": today.isoformat(),
    }
    _save_cache(data)
    logger.info("jp_lotsize更新完了: %d銘柄", len(lot_sizes))
    return lot_sizes
