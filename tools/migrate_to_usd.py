#!/usr/bin/env python3
"""vs-sp500: 円建て→ドル建て移行（一度きり）。SPEC_USD_MOOMOO.md / charter.md v1.4 準拠。

やること（(A)評価通貨を円→ドル、(B)1306.T→EWJ、(C)売買・実費をmoomoo仮想口座に移す）:
    1. 現在価格（yfinance: VOO/QQQ/GLD/IEF/XLV/1306.T）と現在のドル円レートを取得
    2. 現行NAVを円で計算 → 現在のドル円で割ってNAV_usdを得る（移行時点の資産価値を保存）
    3. history.csvをドル建てに書き換える（diff_pctは再計算せず既存値をそのまま使う。
       通貨変換で勝敗比率は不変なため、ここで再計算するとズレが出る）
    4. moomooの実保有を読み、台帳のholdingsをそれと完全一致させる（VOO/QQQ/GLD/IEF/XLV。1306.Tは消える）
    5. cash_usd = NAV_usd − (4で得た保有を現在値で評価した合計)
       （1306.T売却分はEWJを買わずそのままcash_usdに残す。EWJは本スクリプトでは発注しない。
       次回のdaily_run.pyが憲章v1.4のリバランス条件で自然に買う。EWJ 0%vs目標10%＝10pt乖離>5pt
       のため、特別扱いのコードを書かずに既存のリバランスロジックだけで処理される）
    6. trades.csvの既存行（fx_rate/amount_jpy/fee_jpy列）をamount_usd/fee_usd schemaに書き換え、
       移行の記録を追記（rule="usd_migration"）
    7. portfolio.jsonを新形式で保存（cash_usd単一・start_dateとbench_unitsは変更しない）

使い方:
    python3 tools/migrate_to_usd.py --dry-run   # 必ず先に実行し、内容を確認する（書き込みは一切行わない）
    python3 tools/migrate_to_usd.py             # 検証後、1回だけ実行する（冪等でないため複数回実行しないこと）
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
from pathlib import Path

import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import broker  # noqa: E402
import config  # noqa: E402
import portfolio  # noqa: E402

JST = dt.timezone(dt.timedelta(hours=9))

OLD_US_TICKERS = ["VOO", "QQQ", "GLD", "IEF", "XLV"]  # 移行前から保有する米国ETF
JP_TICKER = "1306.T"


class MigrationError(Exception):
    pass


def _fetch_price(ticker: str) -> float:
    hist = yf.Ticker(ticker).history(period="5d", auto_adjust=False)
    if hist.empty:
        raise MigrationError(f"{ticker}: yfinanceから現在価格を取得できなかった")
    return float(hist["Close"].iloc[-1])


def _fetch_usdjpy_history() -> dict[str, float]:
    """日付(YYYY-MM-DD)→USDJPY終値。history.csvの各行の変換に使う。"""
    hist = yf.Ticker(config.FX_TICKER).history(period="2y", auto_adjust=False)
    if hist.empty:
        raise MigrationError("USDJPYの価格履歴を取得できなかった")
    return {idx.date().isoformat(): float(close) for idx, close in hist["Close"].items()}


def _rate_on_or_before(usdjpy_by_date: dict[str, float], target_date: str) -> float:
    """target_date以前で最も新しい日付のUSDJPYレート（祝日等でのデータ欠測に備える）。"""
    candidates = sorted(d for d in usdjpy_by_date if d <= target_date)
    if not candidates:
        raise MigrationError(f"{target_date}以前のUSDJPYレートが見つからない")
    return usdjpy_by_date[candidates[-1]]


def compute_current_nav_jpy(state: dict, prices: dict[str, float], usdjpy_now: float) -> float:
    """移行直前の円建てNAVを、現在の価格・現在のドル円で計算する（旧portfolio.compute_nav_jpy相当）。"""
    nav = state["cash_jpy"] + state.get("cash_usd", 0.0) * usdjpy_now
    for ticker, shares in state["holdings"].items():
        price = prices.get(ticker)
        if price is None:
            raise MigrationError(f"{ticker}の現在価格が無い")
        if ticker == JP_TICKER:
            nav += shares * price
        else:
            nav += shares * price * usdjpy_now
    return nav


def build_new_history_rows(usdjpy_by_date: dict[str, float]) -> list[dict] | None:
    """history.csv（円建て）をドル建てに変換した行リストを返す（ファイルには書き込まない）。

    既にドル建て（nav_usd列）に変換済みなら何もせずNoneを返す（再実行時の冪等性対策。
    2026-08-14: 1回目の移行試行がEWJ約定待ちタイムアウトで中断した際、history.csvだけ
    ドル建てへの書き換えが先に完了していたため、再実行時にこの分岐が必要になった）。
    """
    old_rows = portfolio.read_history_rows()
    if old_rows and "nav_jpy" not in old_rows[0]:
        return None
    new_rows = []
    for row in old_rows:
        date = row["date"]
        rate = _rate_on_or_before(usdjpy_by_date, date)
        new_rows.append({
            "date": date,
            "nav_usd": round(float(row["nav_jpy"]) / rate, 2),
            "bench_usd": round(float(row["bench_jpy"]) / rate, 2),
            "diff_usd": round(float(row["diff_jpy"]) / rate, 2),
            "diff_pct": row["diff_pct"],  # 再計算しない。通貨変換で不変なため既存値をそのまま使う
            "cash_ratio": row["cash_ratio"],
        })
    return new_rows


def write_history_csv(rows: list[dict]) -> None:
    with open(config.HISTORY_CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=portfolio.HISTORY_CSV_HEADER)
        writer.writeheader()
        writer.writerows(rows)


def build_new_trades_rows() -> list[dict] | None:
    """trades.csv（旧: fx_rate/amount_jpy/fee_jpy列）を新schema（amount_usd/fee_usd）に変換する。

    各行が保持している“その取引時点のfx_rate”で円建て金額をドルに割り戻す
    （history.csvの日付ベース変換と違い、行ごとに正確なレートが既にあるため）。
    既に新schema（amount_usd列）ならNoneを返す（冪等性対策）。

    重要: portfolio.append_trade_row()は「ファイルが存在するか」だけでヘッダの要否を
    判断するため、schema変更を伴う移行では新規追記の前に必ずこの関数でファイル全体を
    書き換えること。そうしないと新ヘッダと旧ヘッダのままの既存行が混在し、
    CSVの列がズレて全フィールドが誤った意味で読まれる（2026-08-14に実際に発生し発覚した）。
    """
    old_rows = portfolio.read_trade_rows()
    if old_rows and "amount_usd" in old_rows[0]:
        return None
    new_rows = []
    for row in old_rows:
        fx = float(row["fx_rate"])
        new_rows.append({
            "date": row["date"], "action": row["action"], "ticker": row["ticker"],
            "shares": row["shares"], "price": row["price"], "currency": row["currency"],
            "amount_usd": round(float(row["amount_jpy"]) / fx, 2),
            "fee_usd": round(float(row["fee_jpy"]) / fx, 2),
            "rule": row["rule"], "note": row["note"],
        })
    return new_rows


def write_trades_csv(rows: list[dict]) -> None:
    with open(config.TRADES_CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=portfolio.TRADES_CSV_HEADER)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="vs-sp500: 円建て→ドル建て移行（一度きり）")
    parser.add_argument("--dry-run", action="store_true", help="書き込み・発注を一切行わず、計算結果のみ表示する")
    args = parser.parse_args()

    old_state = portfolio.load_portfolio()
    if "cash_jpy" not in old_state:
        print("既に移行済み（portfolio.jsonにcash_jpyフィールドが無い）。再実行はしない。")
        sys.exit(1)

    print("[1] 現在価格・現在のドル円レートを取得中...")
    price_tickers = OLD_US_TICKERS + [JP_TICKER]
    prices = {t: _fetch_price(t) for t in price_tickers}
    usdjpy_now = _fetch_price(config.FX_TICKER)
    for t, p in prices.items():
        print(f"    {t}: {p}")
    print(f"    USDJPY: {usdjpy_now}")

    print("[2] 現行NAVを計算中...")
    nav_jpy = compute_current_nav_jpy(old_state, prices, usdjpy_now)
    nav_usd = nav_jpy / usdjpy_now
    print(f"    NAV_jpy = {nav_jpy:,.2f}円 → NAV_usd = ${nav_usd:,.2f}（USDJPY={usdjpy_now}）")

    print("[3] history.csvをドル建てに変換中...")
    usdjpy_by_date = _fetch_usdjpy_history()
    new_history_rows = build_new_history_rows(usdjpy_by_date)
    if new_history_rows is None:
        print("    history.csvは既にドル建て（前回試行で変換済み）。再変換はスキップ")
        for r in portfolio.read_history_rows():
            print(f"    {r}")
    else:
        for r in new_history_rows:
            print(f"    {r}")
        if not args.dry_run:
            write_history_csv(new_history_rows)
            print("    history.csv書き込み完了")
        else:
            print("    [dry-run] history.csvへの書き込みはスキップ")

    print("[4] moomooの実保有を取得中...")
    if not broker.is_available():
        raise MigrationError("moomoo(OpenD)に接続できない。中止する")
    broker_positions = broker.get_positions()
    if broker_positions is None:
        raise MigrationError("moomooの保有取得に失敗した。portfolio.jsonは書き換えていない")
    print(f"    moomoo実保有: {broker_positions}")

    print("[5] cash_usdを計算中...")
    # EWJはここでは買わない（次回daily_run.pyがリバランス条件で自然に買う）。
    # 1306.T売却分（NAV_usdの一部）は、5で得た保有（EWJ無し）を差し引いた残りが全てcash_usdになる。
    holdings_value_usd = sum(qty * prices.get(t, 0.0) for t, qty in broker_positions.items())
    cash_usd = nav_usd - holdings_value_usd
    print(f"    保有評価額合計 = ${holdings_value_usd:,.2f} → cash_usd = ${cash_usd:,.2f}")
    if cash_usd < 0:
        raise MigrationError(f"cash_usdが負になった（${cash_usd:,.2f}）。portfolio.jsonは書き換えず中止する")

    if args.dry_run:
        total = cash_usd + holdings_value_usd
        print(
            f"    移行前後NAV一致確認: 移行前NAV_usd=${nav_usd:,.2f} vs "
            f"移行後cash_usd+保有評価額=${total:,.2f} → 差={nav_usd - total:+.6f}"
        )
        print("[dry-run] ここまででエラーは無かった。実行するには --dry-run を外して1回だけ再実行すること。")
        return

    print("[6] trades.csvをドル建てschemaに変換中...")
    new_trades_rows = build_new_trades_rows()
    if new_trades_rows is None:
        print("    trades.csvは既にドル建てschema（前回試行で変換済み）。再変換はスキップ")
    else:
        write_trades_csv(new_trades_rows)
        print(f"    trades.csv書き換え完了（{len(new_trades_rows)}行をamount_usd/fee_usd schemaに変換）")

    print("[6b] trades.csvに移行記録を追記中...")
    today_str = dt.datetime.now(JST).date().isoformat()
    old_jp_shares = old_state["holdings"].get(JP_TICKER, 0.0)
    portfolio.append_trade_row({
        "date": today_str, "action": "SELL", "ticker": JP_TICKER,
        "shares": round(old_jp_shares, 6), "price": round(prices[JP_TICKER], 4), "currency": "JPY",
        "amount_usd": round(old_jp_shares * prices[JP_TICKER] / usdjpy_now, 2), "fee_usd": 0.0,
        "rule": "usd_migration",
        "note": (
            "ドル建て移行に伴う円建てポジションの帳簿上の清算（moomooはJP市場非対応のため実発注ではない）。"
            "売却分はEWJを買わずcash_usdに残す。EWJは次回daily_run.pyのリバランス"
            "（EWJ 0% vs 目標10%＝乖離10pt>5pt）で自然に買われる想定。"
            "また台帳の保有株数をmoomoo実保有（整数）に合わせて再構築し、旧台帳の端株分の差額もcash_usdに算入した"
        ),
    })
    print("    trades.csv追記完了")

    print("[7] portfolio.jsonを新形式で保存中...")
    new_state = {
        "start_date": old_state["start_date"],  # 変更しない（勝敗の基準線）
        "mode": old_state["mode"],
        "cash_usd": round(cash_usd, 6),
        "holdings": {t: float(q) for t, q in broker_positions.items()},
        "bench_units": old_state["bench_units"],  # 変更しない（勝敗の基準線）
        "last_processed_voo_date": old_state["last_processed_voo_date"],
        "below_200dma_streak": old_state["below_200dma_streak"],
        "above_200dma_streak": old_state["above_200dma_streak"],
    }
    portfolio.save_portfolio(new_state)
    print("    portfolio.json保存完了")

    print("[完了] 移行が完了した（EWJは未購入。次回daily_run.pyのリバランスで自然に買われる）。")
    print("       次に `python3 daily_run.py --dry-run --no-loop` で検証すること。")


if __name__ == "__main__":
    try:
        main()
    except MigrationError as e:
        print(f"[エラー] 移行を中止した: {e}", file=sys.stderr)
        sys.exit(1)
