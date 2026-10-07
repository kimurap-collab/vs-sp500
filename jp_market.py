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


def get_splits(ticker: str) -> list[tuple[str, float]] | None:
    """yfinanceから株式分割の履歴を [(分割日 "YYYY-MM-DD", ratio), ...] で返す（2026-10-01追加）。

    ratioは分割後の株数/分割前の株数（yfinanceのsplits値そのもの。1:2なら2.0、5株→1株の併合なら0.2）。
    取得に失敗した場合はNone（呼び出し側はWARNINGを出して分割調整なしで続行する）。
    分割が1件も無い銘柄は空リスト。
    """
    yf_ticker = ticker_to_yf(ticker)
    try:
        splits = yf.Ticker(yf_ticker).splits
    except Exception as e:  # noqa: BLE001 - yfinance内部の例外型は不定
        logger.warning("%s: yfinanceの分割情報の取得に失敗した: %s", yf_ticker, e)
        return None
    if splits is None:
        return None
    return [(idx.date().isoformat(), float(val)) for idx, val in splits.items() if val and float(val) > 0]


def get_info(ticker: str) -> dict | None:
    """yfinanceの.info辞書（sector/industry/marketCap等を含む）を返す（2026-10-07追加・
    JP枠スワップ売却機能）。取得失敗・空の場合はNone（呼び出し側はWARNINGを出して
    その銘柄をTier0扱いで続行する）。
    """
    yf_ticker = ticker_to_yf(ticker)
    try:
        info = yf.Ticker(yf_ticker).info
    except Exception as e:  # noqa: BLE001 - yfinance内部の例外型は不定
        logger.warning("%s: yfinanceのinfo取得に失敗した: %s", yf_ticker, e)
        return None
    if not info:
        logger.warning("%s: yfinanceのinfoが空", yf_ticker)
        return None
    return info


def get_market_caps(tickers: list[str]) -> dict[str, float]:
    """yfinanceのinfo['marketCap']を取得する（2026-10-07追加・JP枠スワップ売却機能）。
    1銘柄の取得失敗は無視して続行する（get_snapshotsと同じ縮退方針）。
    取得できた銘柄だけを含む辞書を返す（全滅してもNoneにはせず空辞書）。
    """
    result: dict[str, float] = {}
    for ticker in tickers:
        info = get_info(ticker)
        cap = info.get("marketCap") if info else None
        if cap is not None and cap == cap and float(cap) > 0:  # cap==capはNaN除外
            result[ticker] = float(cap)
    return result


def get_sector_etf_returns(etf_codes: tuple[str, ...], lookback_trading_days: int) -> dict[str, float] | None:
    """TOPIX-17シリーズETF（銘柄コードのみ。例: "1617"）の直近lookback_trading_days営業日
    リターン(比率)をyfinanceから取得する（2026-10-07追加・JP枠スワップ売却機能。月初回のみ
    呼ぶ想定）。個別ETFの取得失敗はそのETFを戻り値から省く。1件も取得できなければ空辞書
    （呼び出し側が本数不足として扱い、既存キャッシュへフォールバックする）。
    """
    result: dict[str, float] = {}
    for code in etf_codes:
        yf_ticker = ticker_to_yf(code)
        try:
            hist = yf.Ticker(yf_ticker).history(period="3mo", auto_adjust=False)
        except Exception as e:  # noqa: BLE001 - yfinance内部の例外型は不定
            logger.warning("get_sector_etf_returns: %s のhistory取得に失敗した: %s", yf_ticker, e)
            continue
        closes = hist["Close"].dropna().tolist() if not hist.empty else []
        if len(closes) <= lookback_trading_days:
            logger.warning("get_sector_etf_returns: %s の本数不足(%d本)", yf_ticker, len(closes))
            continue
        result[code] = closes[-1] / closes[-1 - lookback_trading_days] - 1.0
    return result


def get_dividends(ticker: str) -> list[tuple[str, float]] | None:
    """yfinanceから1株あたりの現金配当履歴を [(ex_date "YYYY-MM-DD", per_share_jpy), ...] で返す
    （2026-10-07追加・Change3）。取得失敗はNone（呼び出し側はWARNINGを出して記帳なしで続行する）。
    配当が1件も無い銘柄は空リスト。
    """
    yf_ticker = ticker_to_yf(ticker)
    try:
        dividends = yf.Ticker(yf_ticker).dividends
    except Exception as e:  # noqa: BLE001 - yfinance内部の例外型は不定
        logger.warning("%s: yfinanceの配当情報の取得に失敗した: %s", yf_ticker, e)
        return None
    if dividends is None:
        return None
    return [(idx.date().isoformat(), float(val)) for idx, val in dividends.items() if val and float(val) > 0]
