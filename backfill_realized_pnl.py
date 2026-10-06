#!/usr/bin/env python3
"""vs-sp500: 既存SELL行への実現損益(realized_pnl/realized_pnl_pct)バックフィル（一度きり）。

2026-10-06・大将「損切りや利益確定した際の履歴にいくら損や得をしたか書いておいてほしい」
（択一１＝全3枠・２＝台帳とダッシュボード両方・３＝過去の売りもバックフィル。承認:「その他オッケー いけ」）。

本体（ledger/trades.csv）・米国RSI枠（ledger/rsi/trades.csv）・日本株RSI枠（ledger/rsi_jp/trades.csv）
を日付順に再生し、各SELL行の時点の平均取得単価から実現損益を求めて書き戻す。

- 本体: portfolio.compute_avg_costs()と同じ総平均法を銘柄ごとに再生する（手数料fee_usdを差し引く）。
- 米国/日本株RSI枠: lot_id単位でapply_pyramid_fillと同じ加重平均を再生する（手数料は記帳が無いため差し引かない）。

再生後、現在保有中（本体）・未クローズ（RSI枠）の銘柄/ロットについて、再生した平均取得単価が
各portfolio.jsonの値と一致するかを確認する（株式分割をtrades.csvは遡って書き換えない設計のため、
分割を経たロットは一致しないことが既知。強制的に一致させず、不一致として報告するだけに留める）。

使い方:
    python3 backfill_realized_pnl.py --dry-run   # 書き込みなしで差分と整合性チェックの結果だけ表示
    python3 backfill_realized_pnl.py             # trades.csvをバックアップ後に書き換える
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
from pathlib import Path
from typing import Any

import config
import jp_rsi_ledger
import portfolio
import rsi_ledger

BACKUP_DIR = Path(
    "/private/tmp/claude-501/-Users-seijikimura/252d6299-9beb-4598-982c-729f8d008a2d/"
    "scratchpad/pnl_backup"
)


def _to_float(value: str) -> float:
    return float(value) if value not in (None, "") else 0.0


def replay_main_frame_rows(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, float], dict[str, float]]:
    """本体trades.csvの行を日付順(=記帳順)に再生し、SELL行へrealized_pnl/pctを書き込む。

    compute_avg_costs()と同じ総平均法・SELLはavg_costを変えない算法をその場で再現する。
    1株あたり単価は"price"列ではなく"amount_usd"÷"shares"から求める（2026-10-06）。
    通常行(currency=USD)ではprice列と実質同値（丸め誤差未満の差）だが、2026-08-14の
    usd_migration行（1306.T・currency=JPY）だけは"price"がJPY表記のまま残っており、
    "amount_usd"列はその行も含め一貫してドル換算済みのため、これを使うことで
    実現損益の単位をドルに統一できる（compute_avg_costs()自体は現存銘柄のみに使われ
    1306.Tの参照は無いため、本体側は変更していない）。
    戻り値: (更新した行リスト, 銘柄ごとの最終avg_cost, 銘柄ごとの最終株数)
    """
    avg_cost: dict[str, float] = {}
    total_shares: dict[str, float] = {}
    for row in rows:
        ticker = row["ticker"]
        shares = _to_float(row["shares"])
        price = _to_float(row["amount_usd"]) / shares if shares else 0.0
        prev_shares = total_shares.get(ticker, 0.0)
        if row["action"] == "BUY":
            new_shares = prev_shares + shares
            if new_shares > 0:
                prev_avg = avg_cost.get(ticker, 0.0)
                avg_cost[ticker] = (prev_avg * prev_shares + price * shares) / new_shares
            total_shares[ticker] = new_shares
            row["realized_pnl"] = ""
            row["realized_pnl_pct"] = ""
        elif row["action"] == "SELL":
            cost = avg_cost.get(ticker)
            fee = _to_float(row.get("fee_usd", ""))
            if cost:
                row["realized_pnl"] = round((price - cost) * shares - fee, 2)
                row["realized_pnl_pct"] = round((price / cost - 1) * 100, 4)
            else:
                row["realized_pnl"] = ""
                row["realized_pnl_pct"] = ""
            total_shares[ticker] = prev_shares - shares
    return rows, avg_cost, total_shares


def replay_lot_frame_rows(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, float], dict[str, float]]:
    """RSI枠(米国/日本株共通)trades.csvの行をlot_id単位に日付順で再生する。

    apply_pyramid_fillと同じ加重平均をその場で再現する（SELLはavg_costを変えない）。
    手数料は米国RSI枠trades.csvに列が無く・日本株RSI枠はmoomoo発注が無いため差し引かない
    （2026-10-06・大将への報告どおり「fee unknown」）。
    戻り値: (更新した行リスト, lot_idごとの最終avg_cost, lot_idごとの最終株数)
    """
    avg_cost: dict[str, float] = {}
    total_shares: dict[str, float] = {}
    for row in rows:
        lot_id = row["lot_id"]
        shares = _to_float(row["shares"])
        price = _to_float(row["price"])
        prev_shares = total_shares.get(lot_id, 0.0)
        if row["action"] == "BUY":
            new_shares = prev_shares + shares
            if new_shares > 0:
                prev_avg = avg_cost.get(lot_id, 0.0)
                avg_cost[lot_id] = (prev_avg * prev_shares + price * shares) / new_shares
            total_shares[lot_id] = new_shares
            row["realized_pnl"] = ""
            row["realized_pnl_pct"] = ""
        elif row["action"] == "SELL":
            cost = avg_cost.get(lot_id)
            if cost:
                row["realized_pnl"] = round((price - cost) * shares, 2)
                row["realized_pnl_pct"] = round((price / cost - 1) * 100, 4)
            else:
                row["realized_pnl"] = ""
                row["realized_pnl_pct"] = ""
            total_shares[lot_id] = prev_shares - shares
    return rows, avg_cost, total_shares


def _read_rows(path: Path) -> tuple[list[str], list[dict[str, Any]]]:
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    return fieldnames, rows


def _write_rows(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})


def _check_main_consistency(shares: dict[str, float]) -> list[str]:
    problems = []
    with open(config.PORTFOLIO_PATH, encoding="utf-8") as f:
        state = json.load(f)
    holdings = state.get("holdings", {})
    for ticker, qty in holdings.items():
        replayed_qty = shares.get(ticker, 0.0)
        if abs(replayed_qty - qty) > 1e-6:
            problems.append(
                f"本体 {ticker}: 再生株数{replayed_qty} != portfolio.json株数{qty}"
            )
    return problems


def _check_lot_consistency(portfolio_path: Path, avg_cost: dict[str, float]) -> list[str]:
    problems = []
    with open(portfolio_path, encoding="utf-8") as f:
        state = json.load(f)
    for lot in state.get("lots", []):
        if lot.get("closed"):
            continue
        lot_id = lot["lot_id"]
        replayed = avg_cost.get(lot_id)
        stored = lot["avg_cost"]
        if replayed is None or abs(replayed - stored) > max(0.01, abs(stored) * 1e-4):
            problems.append(
                f"{portfolio_path.parent.name} lot={lot_id}: 再生avg_cost={replayed} != "
                f"portfolio.json avg_cost={stored}"
                + (f"（splits_applied={lot['splits_applied']}・既知の分割差）" if lot.get("splits_applied") else "")
            )
    return problems


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    targets = [
        ("本体", config.TRADES_CSV_PATH, replay_main_frame_rows, portfolio.TRADES_CSV_HEADER),
        ("米国RSI枠", config.RSI_TRADES_CSV_PATH, replay_lot_frame_rows, rsi_ledger.RSI_TRADES_CSV_HEADER),
        ("日本株RSI枠", config.RSI_JP_TRADES_CSV_PATH, replay_lot_frame_rows, jp_rsi_ledger.JP_TRADES_CSV_HEADER),
    ]

    if not args.dry_run:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    results: dict[str, tuple[dict[str, float], dict[str, float]]] = {}

    for label, path, replay_fn, header in targets:
        _old_fieldnames, rows = _read_rows(path)
        sell_count = sum(1 for r in rows if r["action"] == "SELL")
        new_rows, avg_cost, shares = replay_fn(rows)
        results[label] = (avg_cost, shares)
        print(f"[{label}] {path}: SELL {sell_count}行にrealized_pnlを計算した")
        if not args.dry_run:
            shutil.copy2(path, BACKUP_DIR / f"{path.parent.name}_{path.name}")
            _write_rows(path, header, new_rows)
            print(f"  → 書き戻し完了（バックアップ: {BACKUP_DIR / f'{path.parent.name}_{path.name}'}）")

    print("\n--- 整合性チェック ---")
    main_problems = _check_main_consistency(results["本体"][1])
    rsi_problems = _check_lot_consistency(config.RSI_PORTFOLIO_PATH, results["米国RSI枠"][0])
    jp_problems = _check_lot_consistency(config.RSI_JP_PORTFOLIO_PATH, results["日本株RSI枠"][0])
    all_problems = main_problems + rsi_problems + jp_problems
    if not all_problems:
        print("不一致なし（全て一致）")
    else:
        for p in all_problems:
            print(f"不一致: {p}")


if __name__ == "__main__":
    main()
