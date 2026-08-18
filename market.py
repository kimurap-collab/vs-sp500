"""vs-sp500: moomooでの市場データ取得（2026-08-18: yfinanceからmoomoo正解主義へ移行。配当は全廃）。

USDJPY（管理画面の参考表示用。評価計算には使わない）だけはmoomooの為替コード体系が
未確認のためyfinanceのまま残す（get_usdjpy_mid参照）。
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import pandas as pd
import yfinance as yf

import broker
import config

logger = logging.getLogger("vs-sp500.market")

MOOMOO_CALL_TIMEOUT_SEC = 15.0  # broker.pyのCALL_TIMEOUT_SECに合わせる
VOO_KLINE_LOOKBACK_DAYS = 500  # 250本以上の日足がないとワイルダー平滑・200日線が収束しないため


@dataclass
class TickerSnapshot:
    ticker: str
    close: float
    date: str  # ISO date (YYYY-MM-DD)


@dataclass
class VooTechnicals:
    ma200: float
    rsi14: float
    high_52w: float
    above_200dma_today: bool
    below_200dma_today: bool


def _run_with_timeout(fn: Callable[[], Any], timeout: float = MOOMOO_CALL_TIMEOUT_SEC) -> Any | None:
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


def _moomoo_snapshot(tickers: list[str]) -> dict[str, TickerSnapshot] | None:
    """get_market_snapshotで終値・日付を取得する（日足取得枠を消費しない）。失敗時None。"""
    if not tickers:
        return {}

    def _call() -> dict[str, TickerSnapshot]:
        from moomoo import OpenQuoteContext

        ctx = OpenQuoteContext(host=config.MOOMOO_HOST, port=config.MOOMOO_PORT)
        try:
            codes = [broker.ticker_to_code(t) for t in tickers]
            ret, data = ctx.get_market_snapshot(codes)
            if ret != 0:
                raise RuntimeError(f"get_market_snapshot失敗: {data}")
            result: dict[str, TickerSnapshot] = {}
            for row in data.to_dict(orient="records"):
                ticker = broker.code_to_ticker(str(row["code"]))
                update_time = str(row.get("update_time") or "")
                date = update_time.split(" ")[0] if update_time else dt.date.today().isoformat()
                result[ticker] = TickerSnapshot(ticker=ticker, close=float(row["last_price"]), date=date)
            return result
        finally:
            ctx.close()

    return _run_with_timeout(_call)


def get_snapshot(ticker: str) -> TickerSnapshot:
    """指定銘柄の直近終値・日付をmoomooから取得する。取得できなければ例外を送出する。"""
    return get_snapshots([ticker])[ticker]


def get_snapshots(tickers: list[str]) -> dict[str, TickerSnapshot]:
    """複数銘柄の直近終値・日付をmoomooから一括取得する。取得できなければ例外を送出する。"""
    snapshot = _moomoo_snapshot(tickers)
    if snapshot is None:
        raise RuntimeError("moomooからの価格取得(get_market_snapshot)に失敗した（タイムアウトまたは例外）")
    missing = sorted(set(tickers) - set(snapshot))
    if missing:
        raise RuntimeError(f"moomooの価格取得結果に銘柄が含まれていない: {missing}")
    return snapshot


def _voo_daily_closes_via_moomoo() -> list[float] | None:
    """VOOの日足終値を取得する（MA200・RSI14・52週高値の計算に1回だけ使い回す）。失敗時None。"""

    def _call() -> list[float]:
        from moomoo import AuType, KLType, OpenQuoteContext

        ctx = OpenQuoteContext(host=config.MOOMOO_HOST, port=config.MOOMOO_PORT)
        try:
            start = (dt.date.today() - dt.timedelta(days=VOO_KLINE_LOOKBACK_DAYS)).isoformat()
            end = dt.date.today().isoformat()
            ret, data, _ = ctx.request_history_kline(
                broker.ticker_to_code(config.BENCHMARK_TICKER),
                start=start, end=end, ktype=KLType.K_DAY, autype=AuType.QFQ,
            )
            if ret != 0:
                raise RuntimeError(f"request_history_kline失敗: {data}")
            return data["close"].astype(float).tolist()
        finally:
            ctx.close()

    return _run_with_timeout(_call)


def _rsi14_wilder(closes: pd.Series) -> float | None:
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / 14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    val = rsi.iloc[-1]
    return None if pd.isna(val) else float(val)


def get_voo_technicals(fallback_last_close: float) -> VooTechnicals:
    """VOOの200日移動平均・RSI14・52週高値をmoomooの日足（1回の取得）から計算する。

    moomoo呼び出しに失敗した場合は安全側の中立値で縮退する
    （RSI14=50.0・MA200と52週高値=fallback_last_close＝押し目買い・200日線判定のどちらも発火しない値）。
    fallback_last_closeは呼び出し側が別途get_market_snapshotで取得済みのVOO終値を渡すこと。
    """
    closes = _voo_daily_closes_via_moomoo()
    if not closes:
        logger.warning("moomooからVOOの日足を取得できなかったため中立値で縮退する")
        return VooTechnicals(
            ma200=fallback_last_close, rsi14=50.0, high_52w=fallback_last_close,
            above_200dma_today=False, below_200dma_today=False,
        )

    close_series = pd.Series(closes)
    last_close = float(close_series.iloc[-1])
    ma200_series = close_series.rolling(200).mean()
    ma200 = float(ma200_series.iloc[-1]) if not pd.isna(ma200_series.iloc[-1]) else float(close_series.mean())
    rsi14 = _rsi14_wilder(close_series)
    if rsi14 is None:
        logger.warning("VOOのRSI14計算結果がNaN（データ不足）のため中立値(50.0)を使う")
        rsi14 = 50.0
    high_52w = float(close_series.tail(252).max())
    return VooTechnicals(
        ma200=ma200,
        rsi14=rsi14,
        high_52w=high_52w,
        above_200dma_today=last_close > ma200,
        below_200dma_today=last_close < ma200,
    )


def _fx_history(ticker: str, period: str = "3mo") -> pd.DataFrame:
    """USDJPY=Xの価格履歴をyfinanceで取得する（管理画面の参考表示用。評価計算には使わない）。"""
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


def get_usdjpy_mid() -> TickerSnapshot:
    """USDJPYの仲値（直近終値）を取得する（yfinance。管理画面の参考表示用）。"""
    hist = _fx_history(config.FX_TICKER)
    last_close = float(hist["Close"].iloc[-1])
    last_date = hist.index[-1].date().isoformat()
    return TickerSnapshot(ticker=config.FX_TICKER, close=last_close, date=last_date)
