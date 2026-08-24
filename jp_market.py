"""vs-sp500: 日本株RSI枠の市場データ取得（yfinance）。

moomooはjp_stock_qot_right: NOのため日本株の相場取得(get_market_snapshot等)が
権限エラーになる（2026-08-24実機確認）。yfinanceはmoomooの株価と全銘柄で一致することを
検証済みのため、保有銘柄の日々の価格取得はyfinanceで行う（候補抽出はmoomooスクリーナー
が権限不要で使えるためjp_rsi_daily.py側で別途扱う）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import yfinance as yf

import config

logger = logging.getLogger("vs-sp500.jp_market")


@dataclass
class JpSnapshot:
    ticker: str
    close: float
    date: str


def ticker_to_yf(ticker: str) -> str:
    """台帳ティッカー（例: '6367'）をyfinanceのティッカー（例: '6367.T'）に変換する。"""
    return f"{ticker}.T"


def get_jp_trading_date() -> str:
    """日本の直近取引日を判定する（自前の時差計算はせず、1306.T(TOPIX ETF)の最新の
    完成した日足の日付をそのまま使う。大将の指示どおり）。"""
    hist = yf.Ticker(config.JP_TRADING_DAY_TICKER).history(period="5d", auto_adjust=False)
    if hist.empty:
        raise RuntimeError(f"{config.JP_TRADING_DAY_TICKER}: yfinanceから日足を取得できなかった")
    return hist.index[-1].date().isoformat()


def get_snapshots(tickers: list[str]) -> dict[str, JpSnapshot]:
    """保有銘柄の直近終値をyfinanceから取得する。

    1銘柄の取得失敗は無視して続行する（rsi_daily.fetch_market_dataと同じ縮退方針。
    1銘柄の欠測でNAV計算全体を止めないため）。
    """
    result: dict[str, JpSnapshot] = {}
    for ticker in tickers:
        yf_ticker = ticker_to_yf(ticker)
        try:
            hist = yf.Ticker(yf_ticker).history(period="5d", auto_adjust=False)
            if hist.empty:
                logger.warning("%s: yfinanceの価格データが空", yf_ticker)
                continue
            close = float(hist["Close"].iloc[-1])
            date = hist.index[-1].date().isoformat()
            result[ticker] = JpSnapshot(ticker=ticker, close=close, date=date)
        except Exception as e:  # noqa: BLE001 - yfinance内部の例外型は不定
            logger.warning("%s: yfinance取得に失敗した: %s", yf_ticker, e)
    return result
