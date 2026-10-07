#!/usr/bin/env python3
"""vs-sp500: 米国/日本株RSI枠への配当バックフィル（一度きり。2026-10-07・Change3）。

各ロットの建て日以降の全ての過去ex-dateについて、保有していた期間分の配当を遡って
dividends.csvへ記帳し、現在のcash_usd/cash_jpyへ合計額を加算する。history.csvは
最初に配当が発生した日付以降の行について、その日までの累計配当額をnavへ加算して
再計算する（配当は遡って再投資しない。ベンチマーク/元本列は変えない）。

本体（portfolio.py）は対象外（大将「配当は不要だと言っている」charter v1.6）。

使い方:
    python3 backfill_dividends.py --dry-run   # 書き込みなしで記帳予定・NAV変化だけ表示
    python3 backfill_dividends.py             # バックアップ後にdividends.csv/portfolio.json/history.csvを書き換える
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

import broker
import config
import dividends
import jp_market
import jp_rsi_ledger
import rsi_ledger

BACKUP_DIR = Path(
    "/private/tmp/claude-501/-Users-seijikimura/252d6299-9beb-4598-982c-729f8d008a2d/"
    "scratchpad/fix_backup"
)


def _fetch_us_dividends(tickers: list[str]) -> dict[str, list[tuple[str, float]]]:
    result = broker.get_dividends(tickers)
    if result is None:
        raise RuntimeError("moomoo(get_rehab)から配当情報が取得できなかった。中断する。")
    missing = [t for t in tickers if t not in result]
    if missing:
        print(f"  警告: 配当情報が取得できなかった銘柄: {', '.join(missing)}")
    return result


def _fetch_jp_dividends(tickers: list[str]) -> dict[str, list[tuple[str, float]]]:
    result: dict[str, list[tuple[str, float]]] = {}
    missing = []
    for ticker in tickers:
        divs = jp_market.get_dividends(ticker)
        if divs is None:
            missing.append(ticker)
            continue
        result[ticker] = divs
    if missing:
        print(f"  警告: 配当情報が取得できなかった銘柄: {', '.join(missing)}")
    return result


def _recompute_history(
    history_rows: list[dict[str, Any]],
    new_div_rows: list[dict[str, Any]],
    nav_key: str,
    diff_key: str,
    bench_or_principal_key: str,
) -> tuple[list[dict[str, Any]], str | None, float, float]:
    """new_div_rowsのex_date以降のhistory行に累計配当額をnavへ加算する。

    戻り値: (更新後のhistory_rows, 最初に影響を受けた日付（無ければNone）,
             最古の影響行のnav変化前, 変化後)。最新行のnav前後は呼び出し側がhistory_rowsから算出する。
    """
    if not new_div_rows:
        return history_rows, None, 0.0, 0.0
    div_dates = sorted({r["date"] for r in new_div_rows})
    first_date = div_dates[0]
    sorted_divs = sorted((r["date"], r["amount"]) for r in new_div_rows)

    updated = []
    before_after_first = (0.0, 0.0)
    seen_first = False
    for row in sorted(history_rows, key=lambda r: r["date"]):
        cumulative = sum(amount for d, amount in sorted_divs if d <= row["date"])
        if cumulative <= 0:
            updated.append(row)
            continue
        old_nav = float(row[nav_key])
        new_nav = old_nav + cumulative
        bench_or_principal = float(row[bench_or_principal_key])
        new_diff = new_nav - bench_or_principal
        new_row = dict(row)
        new_row[nav_key] = new_nav
        new_row[diff_key] = new_diff
        new_row["diff_pct"] = (new_diff / bench_or_principal * 100.0) if bench_or_principal else 0.0
        updated.append(new_row)
        if not seen_first:
            before_after_first = (old_nav, new_nav)
            seen_first = True
    return updated, first_date, before_after_first[0], before_after_first[1]


def _write_history_csv(path: Path, header: list[str], rows: list[dict[str, Any]]) -> None:
    import csv

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        writer.writeheader()
        for row in sorted(rows, key=lambda r: r["date"]):
            writer.writerow({k: row.get(k, "") for k in header})


def run_frame(
    label: str,
    portfolio_path: Path,
    history_path: Path,
    history_header: list[str],
    nav_key: str,
    diff_key: str,
    bench_or_principal_key: str,
    cash_key: str,
    lots: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    existing_dividend_rows: list[dict[str, Any]],
    dividends_by_ticker: dict[str, list[tuple[str, float]]],
    source: str,
    append_dividend_row_fn,
    dry_run: bool,
) -> None:
    existing_keys = {(r["ticker"], r["date"], r["lot_id"]) for r in existing_dividend_rows}
    new_rows = dividends.compute_new_dividend_credits(lots, trades, dividends_by_ticker, existing_keys, source)
    total = round(sum(r["amount"] for r in new_rows), 2)
    print(f"[{label}] 新規記帳対象: {len(new_rows)}件・合計 {total:,.2f}")
    if not new_rows:
        return

    with open(history_path, encoding="utf-8") as f:
        import csv

        history_rows = list(csv.DictReader(f))
    for row in history_rows:
        row[nav_key] = float(row[nav_key])
        row[diff_key] = float(row[diff_key])
        row[bench_or_principal_key] = float(row[bench_or_principal_key])
        row["diff_pct"] = float(row["diff_pct"])

    updated_history, first_date, nav_before, nav_after = _recompute_history(
        history_rows, new_rows, nav_key, diff_key, bench_or_principal_key,
    )
    latest_before = history_rows[-1][nav_key] if history_rows else 0.0
    latest_after = (
        sorted(updated_history, key=lambda r: r["date"])[-1][nav_key] if updated_history else 0.0
    )
    print(f"  影響開始日: {first_date}・最古の影響行NAV: {nav_before:,.2f} → {nav_after:,.2f}")
    print(f"  最新行NAV: {latest_before:,.2f} → {latest_after:,.2f}")

    if dry_run:
        return

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(portfolio_path, BACKUP_DIR / f"{portfolio_path.parent.name}_{portfolio_path.name}.divbak")
    shutil.copy2(history_path, BACKUP_DIR / f"{history_path.parent.name}_{history_path.name}.divbak")

    for row in new_rows:
        append_dividend_row_fn(row)

    with open(portfolio_path, encoding="utf-8") as f:
        state = json.load(f)
    state[cash_key] = state.get(cash_key, 0.0) + total
    tmp_path = portfolio_path.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp_path.replace(portfolio_path)

    _write_history_csv(history_path, history_header, updated_history)
    print(f"  → dividends.csv({len(new_rows)}行)・portfolio.json(cash+{total:,.2f})・history.csv を更新した")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with open(config.RSI_PORTFOLIO_PATH, encoding="utf-8") as f:
        us_state = json.load(f)
    with open(config.RSI_JP_PORTFOLIO_PATH, encoding="utf-8") as f:
        jp_state = json.load(f)

    us_lots = us_state.get("lots", [])
    jp_lots = jp_state.get("lots", [])
    us_tickers = sorted({lot["ticker"] for lot in us_lots})
    jp_tickers = sorted({lot["ticker"] for lot in jp_lots})

    print(f"米国RSI枠: 対象ティッカー{len(us_tickers)}件・ロット{len(us_lots)}件")
    print(f"日本株RSI枠: 対象ティッカー{len(jp_tickers)}件・ロット{len(jp_lots)}件")

    us_dividends = _fetch_us_dividends(us_tickers) if us_tickers else {}
    jp_dividends = _fetch_jp_dividends(jp_tickers) if jp_tickers else {}

    run_frame(
        "米国RSI枠", config.RSI_PORTFOLIO_PATH, config.RSI_HISTORY_CSV_PATH,
        rsi_ledger.RSI_HISTORY_CSV_HEADER, "nav_usd", "diff_usd", "bench_usd", "cash_usd",
        us_lots, rsi_ledger.read_trade_rows(), rsi_ledger.read_dividend_rows(),
        us_dividends, "moomoo", rsi_ledger.append_dividend_row, args.dry_run,
    )
    run_frame(
        "日本株RSI枠", config.RSI_JP_PORTFOLIO_PATH, config.RSI_JP_HISTORY_CSV_PATH,
        jp_rsi_ledger.JP_HISTORY_CSV_HEADER, "nav_jpy", "diff_jpy", "principal_jpy", "cash_jpy",
        jp_lots, jp_rsi_ledger.read_trade_rows(), jp_rsi_ledger.read_dividend_rows(),
        jp_dividends, "yfinance", jp_rsi_ledger.append_dividend_row, args.dry_run,
    )


if __name__ == "__main__":
    main()
