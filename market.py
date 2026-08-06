"""vs-sp500: yfinanceでの市場データ取得。"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import pandas as pd
import yfinance as yf

import config


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
    t = yf.Ticker(ticker)
    hist = t.history(period=period, auto_adjust=False)
    if hist.empty:
        raise RuntimeError(f"{ticker}: yfinanceから価格データを取得できなかった")
    return hist


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


def compute_rsi14(closes: pd.Series) -> float:
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    val = rsi.iloc[-1]
    if pd.isna(val):
        return 50.0  # データ不足時は中立値
    return float(val)


def get_voo_technicals() -> VooTechnicals:
    """VOOの200日移動平均・RSI14・52週高値を計算する。"""
    hist = _history(config.BENCHMARK_TICKER, period="2y")
    close = hist["Close"]
    last_close = float(close.iloc[-1])
    ma200_series = close.rolling(200).mean()
    ma200 = float(ma200_series.iloc[-1]) if not pd.isna(ma200_series.iloc[-1]) else float(close.mean())
    rsi14 = compute_rsi14(close)
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
