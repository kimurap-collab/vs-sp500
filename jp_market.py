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

    yfinanceのhistory(period='5d')は日本株(.T)について、Yahooの複数日レンジエンドポイントの
    反映遅延により最新営業日の行をClose=NaNで返すことがある（2026-08-26 02:00 JST実測）。
    そのため5dの最新行がNaNの場合に限りperiod='1d'を追加取得して補完する。
    """
    result: dict[str, JpSnapshot] = {}
    for ticker in tickers:
        yf_ticker = ticker_to_yf(ticker)
        try:
            t = yf.Ticker(yf_ticker)
            hist = t.history(period="5d", auto_adjust=False)
            if hist.empty:
                logger.warning("%s: yfinanceの価格データが空", yf_ticker)
                continue

            raw_last_date = hist.index[-1].date()
            valid_hist = hist.dropna(subset=["Close"])
            close: float | None = None
            valid_date = None
            if not valid_hist.empty:
                close = float(valid_hist["Close"].iloc[-1])
                valid_date = valid_hist.index[-1].date()

            # 5dの最新バーがNaNだった場合（=有効な最新日付がrawの最終行より古い、または
            # 有効な行が1つも無い場合）のみ1dで補完する。
            if valid_date is None or valid_date < raw_last_date:
                hist_1d = t.history(period="1d", auto_adjust=False)
                valid_1d = hist_1d if hist_1d.empty else hist_1d.dropna(subset=["Close"])
                if not valid_1d.empty:
                    date_1d = valid_1d.index[-1].date()
                    if valid_date is None or date_1d > valid_date:
                        close = float(valid_1d["Close"].iloc[-1])
                        valid_date = date_1d
                        logger.info(
                            "%s: 5dの最新バーがNaNのため1dで補完（採用日付=%s）",
                            yf_ticker,
                            valid_date.isoformat(),
                        )

            if close is None or valid_date is None:
                logger.warning("%s: yfinanceの終値データが全てNaN", yf_ticker)
                continue

            result[ticker] = JpSnapshot(ticker=ticker, close=close, date=valid_date.isoformat())
        except Exception as e:  # noqa: BLE001 - yfinance内部の例外型は不定
            logger.warning("%s: yfinance取得に失敗した: %s", yf_ticker, e)
    return result
