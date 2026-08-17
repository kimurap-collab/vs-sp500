"""vs-sp500: RSI-30枠の対象ユニバース（S&P500 + NASDAQ100）取得・キャッシュ。

SPEC_RSI30.md準拠。moomooのget_plate_stockを試し、取得できなければ他の経路
（Wikipedia）で取得する。結果はuniverse.jsonにキャッシュし、月初のみ更新する。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import threading
from io import StringIO
from typing import Any, Callable

import pandas as pd
import requests

import config

logger = logging.getLogger("vs-sp500.universe")

_HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; vs-sp500-universe-fetch/1.0)"}
_HTTP_TIMEOUT_SEC = 20.0
_MOOMOO_TIMEOUT_SEC = 15.0

SP500_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
NASDAQ100_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies"

# S&P500(約500)+NASDAQ100(約100、重複除く)は最低でもこの件数を超えるはず。
# moomooのプレートは「構成比上位10社」等の紛らわしい概念プレートを拾うことがあるため
# (実機確認: US.LIST23560 "Top 10 S&P 500 companies by weight"のみヒットし10銘柄しか返らなかった)、
# この件数を下回るmoomoo結果は誤ヒットとみなし採用しない。
MOOMOO_RESULT_MIN_SIZE = 400


def _normalize_ticker(raw: str) -> str:
    """yfinance/moomoo表記に正規化する（例: BRK.B → BRK-B）。"""
    return raw.strip().upper().replace(".", "-")


def _run_with_timeout(fn: Callable[[], Any], timeout: float) -> Any | None:
    """broker.pyと同じ方式: デーモンスレッドで実行しtimeoutで見切りをつける。"""
    result: dict[str, Any] = {}
    error: dict[str, Exception] = {}

    def _target() -> None:
        try:
            result["value"] = fn()
        except Exception as e:  # noqa: BLE001 - SDK内部の例外型は不定
            error["value"] = e

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    if thread.is_alive():
        logger.warning("ユニバース取得の呼び出しがタイムアウトした（%s秒）", timeout)
        return None
    if "value" in error:
        logger.warning("ユニバース取得の呼び出しが例外を送出した: %s", error["value"])
        return None
    return result.get("value")


def _fetch_via_moomoo() -> list[str] | None:
    """moomooのget_plate_stockでS&P500・NASDAQ100構成銘柄を取得する（ベストエフォート）。

    プレートコードが環境依存で不確実なため、get_plate_listでプレート一覧から
    名称に"S&P 500"・"NASDAQ-100"を含むものを検索して使う。見つからない・
    タイムアウト・例外時はNoneを返し、呼び出し側がWikipediaへフォールバックする。
    """

    def _call() -> list[str] | None:
        from moomoo import Market, OpenQuoteContext, Plate

        ctx = OpenQuoteContext(host=config.MOOMOO_HOST, port=config.MOOMOO_PORT)
        try:
            ret, plates = ctx.get_plate_list(market=Market.US, plate_class=Plate.ALL)
            if ret != 0 or plates is None or plates.empty:
                return None
            tickers: set[str] = set()
            for _, row in plates.iterrows():
                name = str(row.get("plate_name", ""))
                if "S&P 500" not in name and "NASDAQ-100" not in name and "NASDAQ 100" not in name:
                    continue
                code = row.get("code")
                ret2, stocks = ctx.get_plate_stock(code)
                if ret2 != 0 or stocks is None or stocks.empty:
                    continue
                for stock_code in stocks["code"]:
                    ticker = str(stock_code).split(".", 1)[1] if "." in str(stock_code) else str(stock_code)
                    tickers.add(_normalize_ticker(ticker))
            return sorted(tickers) if tickers else None
        finally:
            ctx.close()

    return _run_with_timeout(_call, _MOOMOO_TIMEOUT_SEC)


def _fetch_sp500_from_wikipedia() -> list[str]:
    resp = requests.get(SP500_WIKI_URL, headers=_HTTP_HEADERS, timeout=_HTTP_TIMEOUT_SEC)
    resp.raise_for_status()
    tables = pd.read_html(StringIO(resp.text))
    df = tables[0]
    return [_normalize_ticker(t) for t in df["Symbol"].tolist()]


def _fetch_nasdaq100_from_wikipedia() -> list[str]:
    resp = requests.get(NASDAQ100_WIKI_URL, headers=_HTTP_HEADERS, timeout=_HTTP_TIMEOUT_SEC)
    resp.raise_for_status()
    tables = pd.read_html(StringIO(resp.text))
    df = tables[0]
    return [_normalize_ticker(t) for t in df["Ticker"].tolist()]


def _fetch_via_wikipedia() -> tuple[list[str], str]:
    sp500 = _fetch_sp500_from_wikipedia()
    nasdaq100 = _fetch_nasdaq100_from_wikipedia()
    combined = sorted(set(sp500) | set(nasdaq100))
    return combined, f"Wikipedia (S&P500 {len(sp500)}銘柄 + NASDAQ100 {len(nasdaq100)}銘柄 → 重複排除{len(combined)}銘柄)"


def _load_cache() -> dict[str, Any] | None:
    if not config.RSI_UNIVERSE_PATH.exists():
        return None
    try:
        with open(config.RSI_UNIVERSE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("universe.json読み込み失敗: %s", e)
        return None


def _save_cache(data: dict[str, Any]) -> None:
    config.RSI_UNIVERSE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = config.RSI_UNIVERSE_PATH.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp_path.replace(config.RSI_UNIVERSE_PATH)


def _needs_refresh(cache: dict[str, Any] | None, today: dt.date) -> bool:
    if cache is None or not cache.get("tickers"):
        return True
    try:
        fetched_date = dt.date.fromisoformat(cache["fetched_date"])
    except (KeyError, ValueError):
        return True
    # 月初のみ更新: キャッシュの取得月が当月と違えば更新する
    return (fetched_date.year, fetched_date.month) != (today.year, today.month)


def get_universe(today: dt.date | None = None, force_refresh: bool = False) -> list[str]:
    """S&P500+NASDAQ100の構成銘柄（重複排除・正規化済み）を返す。

    月初のみ再取得し、それ以外はuniverse.jsonのキャッシュをそのまま返す。
    """
    today = today or dt.date.today()
    cache = _load_cache()
    if not force_refresh and not _needs_refresh(cache, today):
        return list(cache["tickers"])

    tickers = _fetch_via_moomoo()
    if tickers and len(tickers) >= MOOMOO_RESULT_MIN_SIZE:
        source = f"moomoo get_plate_stock ({len(tickers)}銘柄)"
    else:
        if tickers:
            logger.warning(
                "moomooのプレート結果が%d銘柄しか無く誤ヒットの疑いがあるためWikipediaへフォールバックする",
                len(tickers),
            )
        tickers, source = _fetch_via_wikipedia()

    data = {
        "tickers": tickers,
        "source": source,
        "fetched_date": today.isoformat(),
    }
    _save_cache(data)
    logger.info("ユニバース更新完了: %d銘柄（出典: %s）", len(tickers), source)
    return tickers
