"""vs-sp500: RSI枠（米国・日本株）の配当記帳ロジック（純粋関数のみ。broker呼び出し・ファイルI/Oを含まない）。

2026-10-07追加（Change3）。本体（portfolio.py）は対象外（大将「配当は不要だと言っている」charter v1.6）。

配当記帳のルール:
- 権利確定日(ex-date)の前日終値時点で保有していた株数 × 1株あたり配当額をcash扱いで記帳する。
- 同じ(ticker, ex_date, lot_id)は二重記帳しない（再実行・毎日のジョブ実行で冪等）。
- 「ex_dateの前日終値時点の保有株数」はtrades.csv（そのlot_idのBUY/SELL行）をex_date未満の日付で
  再生して求める。株式分割はtrades.csvに行を増やさず lot の shares を直接書き換えるだけのため
  （rsi_strategy.apply_split）、trades.csvの再生結果は分割の影響を受けない「その時点の実株数」に
  一致する（配当per_share側もデータソースが実株数基準の未調整額を返すため、分割調整は不要）。
"""
from __future__ import annotations

from typing import Any


def shares_held_before_date(lot_trades_sorted: list[dict[str, Any]], ex_date: str) -> float:
    """ex_date未満の日付のBUY/SELL行だけを日付順に再生し、ex_dateの前日終値時点の保有株数を返す。

    lot_trades_sorted: 対象lot_idのtrades.csv行（dateで昇順ソート済みのこと）。
    """
    shares = 0.0
    for row in lot_trades_sorted:
        if row["date"] >= ex_date:
            break
        qty = float(row["shares"])
        if row["action"] == "BUY":
            shares += qty
        elif row["action"] == "SELL":
            shares -= qty
    return shares


def compute_new_dividend_credits(
    lots: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    dividends_by_ticker: dict[str, list[tuple[str, float]]],
    existing_keys: set[tuple[str, str, str]],
    source: str,
) -> list[dict[str, Any]]:
    """未記帳の配当を(ticker, ex_date, lot_id)単位で洗い出し、dividends.csv行の形で返す。

    lots: portfolio.jsonの"lots"配列（クローズ済み含む。保有していた期間分も遡って記帳するため）。
    trades: trades.csvの全行（dict・文字列のまま）。
    dividends_by_ticker: {ticker: [(ex_date, per_share), ...]}（per_share<=0の行は無視）。
    existing_keys: 既にdividends.csvに記帳済みの(ticker, ex_date, lot_id)集合。
    source: "moomoo" | "yfinance"（記帳行のsource列に入れる）。
    """
    trades_by_lot: dict[str, list[dict[str, Any]]] = {}
    for row in trades:
        trades_by_lot.setdefault(row["lot_id"], []).append(row)
    for lot_trades in trades_by_lot.values():
        lot_trades.sort(key=lambda r: r["date"])

    new_rows: list[dict[str, Any]] = []
    for lot in lots:
        ticker = lot["ticker"]
        events = dividends_by_ticker.get(ticker)
        if not events:
            continue
        lot_trades = trades_by_lot.get(lot["lot_id"], [])
        for ex_date, per_share in events:
            if per_share is None or per_share <= 0:
                continue
            key = (ticker, ex_date, lot["lot_id"])
            if key in existing_keys:
                continue
            shares = shares_held_before_date(lot_trades, ex_date)
            if shares <= 0:
                continue
            amount = round(shares * per_share, 2)
            if amount <= 0:
                continue
            new_rows.append({
                "date": ex_date,
                "ticker": ticker,
                "lot_id": lot["lot_id"],
                "shares": shares,
                "per_share": per_share,
                "amount": amount,
                "source": source,
            })
    return new_rows
