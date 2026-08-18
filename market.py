"""vs-sp500: yfinanceでの市場データ取得。"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import pandas as pd
import yfinance as yf

import config

logger = logging.getLogger("vs-sp500.market")

VOO_RSI_MOOMOO_TIMEOUT_SEC = 15.0  # broker.pyのCALL_TIMEOUT_SECに合わせる
VOO_RSI_LOOKBACK_DAYS = 500  # 250本以上の日足がないとワイルダー平滑が収束しないため


@dataclass
class TickerSnapshot:
    ticker: str
    close: float
    date: str  # ISO date (YYYY-MM-DD)
    dividend: float = 0.0  # 当日配当（1株あたり）


@dataclass
class VooTechnicals:
    ma200: float
    rsi14: float
    high_52w: float
    above_200dma_today: bool
    below_200dma_today: bool


def _history(ticker: str, period: str = "1y") -> pd.DataFrame:
    """価格履歴を取得する。yfinance側の一時的な応答不良に備えてリトライする。

    2026-08-11・08-12にYahoo APIが空レスポンスを返し（chart=None → TypeError）、
    その日の実行が丸ごと落ちて台帳が欠測した。同じ理由で1日を失わないための備え。
    """
    last_error: Exception | None = None
    for attempt in range(config.MARKET_FETCH_RETRIES):
        try:
            hist = yf.Ticker(ticker).history(period=period, auto_adjust=False)
            if not hist.empty:
                return hist
            last_error = RuntimeError("価格データが空")
        except Exception as e:  # noqa: BLE001 - yfinance内部の例外型は不定
            last_error = e
        if attempt < config.MARKET_FETCH_RETRIES - 1:
            time.sleep(config.MARKET_FETCH_BACKOFF_SEC * (attempt + 1))
    raise RuntimeError(
        f"{ticker}: yfinanceから価格データを取得できなかった"
        f"（{config.MARKET_FETCH_RETRIES}回試行）: {last_error}"
    )


def _dividend_on_last_date(ticker: str, hist: pd.DataFrame) -> float:
    """直近終値日にあった配当（1株あたり）。無ければ0.0。"""
    divs = hist.get("Dividends")
    if divs is None or divs.empty:
        return 0.0
    last_date = hist.index[-1]
    val = divs.get(last_date)
    if val is None:
        return 0.0
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def get_snapshot(ticker: str) -> TickerSnapshot:
    """指定銘柄の直近終値・日付・当日配当を取得する。"""
    hist = _history(ticker, period="3mo")
    last_close = float(hist["Close"].iloc[-1])
    last_date = hist.index[-1].date().isoformat()
    dividend = _dividend_on_last_date(ticker, hist)
    return TickerSnapshot(ticker=ticker, close=last_close, date=last_date, dividend=dividend)


def get_snapshots(tickers: list[str]) -> dict[str, TickerSnapshot]:
    result: dict[str, TickerSnapshot] = {}
    for tk in tickers:
        result[tk] = get_snapshot(tk)
    return result


def _run_with_timeout(fn: Callable[[], Any], timeout: float = VOO_RSI_MOOMOO_TIMEOUT_SEC) -> Any | None:
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
        logger.error("moomoo呼び出し（VOO RSI取得）がタイムアウトした（%s秒）", timeout)
        return None
    if "value" in error:
        logger.error("moomoo呼び出し（VOO RSI取得）が例外を送出した: %s", error["value"])
        return None
    return result.get("value")


def _voo_rsi14_via_moomoo() -> float | None:
    """moomooの日足からVOOのRSI(14)をワイルダー平滑で計算する。失敗時None。"""

    def _call() -> float:
        from moomoo import AuType, KLType, OpenQuoteContext

        ctx = OpenQuoteContext(host=config.MOOMOO_HOST, port=config.MOOMOO_PORT)
        try:
            start = (dt.date.today() - dt.timedelta(days=VOO_RSI_LOOKBACK_DAYS)).isoformat()
            end = dt.date.today().isoformat()
            ret, data, _ = ctx.request_history_kline(
                "US.VOO", start=start, end=end, ktype=KLType.K_DAY, autype=AuType.QFQ,
            )
            if ret != 0:
                raise RuntimeError(f"request_history_kline失敗: {data}")
            closes = pd.Series(data["close"].astype(float).tolist())
            delta = closes.diff()
            gain = delta.clip(lower=0)
            loss = -delta.clip(upper=0)
            avg_gain = gain.ewm(alpha=1 / 14, adjust=False).mean()
            avg_loss = loss.ewm(alpha=1 / 14, adjust=False).mean()
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))
            val = rsi.iloc[-1]
            if pd.isna(val):
                raise RuntimeError("RSI計算結果がNaN（データ不足）")
            return float(val)
        finally:
            ctx.close()

    return _run_with_timeout(_call)


def get_voo_rsi14() -> float:
    """VOOのRSI(14)をmoomooから取得する（ワイルダー平滑・前復権）。

    取得できなかった場合は50.0（中立値）を返す。押し目買いはRSI<30が発動条件のため、
    50.0なら発動しない＝安全側に倒れる。
    """
    val = _voo_rsi14_via_moomoo()
    if val is None:
        logger.warning("moomooからVOOのRSI14を取得できなかったため50.0を返す")
        return 50.0
    return val


def get_voo_technicals() -> VooTechnicals:
    """VOOの200日移動平均・RSI14・52週高値を計算する。

    200日移動平均・52週高値はyfinanceのまま（ベンチマークの採点基準を変えないため）。
    RSI14のみmoomooから取得する（moomoo正解主義。2026-08-18指示）。
    """
    hist = _history(config.BENCHMARK_TICKER, period="2y")
    close = hist["Close"]
    last_close = float(close.iloc[-1])
    ma200_series = close.rolling(200).mean()
    ma200 = float(ma200_series.iloc[-1]) if not pd.isna(ma200_series.iloc[-1]) else float(close.mean())
    rsi14 = get_voo_rsi14()
    high_52w = float(close.tail(252).max())
    return VooTechnicals(
        ma200=ma200,
        rsi14=rsi14,
        high_52w=high_52w,
        above_200dma_today=last_close > ma200,
        below_200dma_today=last_close < ma200,
    )


def get_usdjpy_mid() -> TickerSnapshot:
    """USDJPYの仲値（直近終値）を取得する。"""
    return get_snapshot(config.FX_TICKER)
