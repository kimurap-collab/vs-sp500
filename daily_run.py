#!/usr/bin/env python3
"""vs-sp500: 毎朝実行のエントリポイント。"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import subprocess
import sys

import config
import market
import portfolio
import report
from advisor import get_trade_decision

JST = dt.timezone(dt.timedelta(hours=9))


def setup_logging() -> logging.Logger:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("vs-sp500")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        fh = logging.FileHandler(config.LOG_DIR / "daily_run.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(fh)
    return logger


def git_commit_and_push(now_jst: dt.datetime, logger: logging.Logger) -> None:
    date_str = now_jst.strftime("%Y-%m-%d")
    try:
        subprocess.run(["git", "add", "ledger/", "data.json"], cwd=config.BASE_DIR, check=True, capture_output=True)
        result = subprocess.run(
            ["git", "commit", "-m", f"update {date_str}"],
            cwd=config.BASE_DIR, capture_output=True, text=True,
        )
        if result.returncode != 0 and "nothing to commit" not in result.stdout:
            logger.warning("git commit警告: %s", result.stdout + result.stderr)
        subprocess.run(["git", "push"], cwd=config.BASE_DIR, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        logger.error("git commit/push失敗: %s", e)


def run(dry_run: bool = False) -> str:
    logger = setup_logging()
    now_jst = dt.datetime.now(JST)
    report_lines: list[str] = []

    def log_and_report(msg: str) -> None:
        logger.info(msg)
        report_lines.append(msg)

    try:
        state = portfolio.load_portfolio()
        charter_text = portfolio.load_charter_text()
        charter_targets = portfolio.parse_charter_targets(charter_text)
        log_and_report(f"[1] 台帳読み込み完了。mode={state['mode']} start_date={state['start_date']}")

        tickers_to_fetch = sorted(set(list(config.WHITELIST.keys()) + list(state["holdings"].keys())))
        snapshots = market.get_snapshots(tickers_to_fetch)
        usdjpy_snap = market.get_usdjpy_mid()
        usdjpy_mid = usdjpy_snap.close
        voo_technicals = market.get_voo_technicals()
        voo_snap = snapshots[config.BENCHMARK_TICKER]
        log_and_report(
            f"[2] 市場データ取得完了。VOO終値={voo_snap.close}({voo_snap.date}) "
            f"USDJPY={usdjpy_mid} MA200={voo_technicals.ma200:.2f} RSI14={voo_technicals.rsi14:.1f} "
            f"52w高値={voo_technicals.high_52w:.2f}"
        )

        # 3. 初回判定
        if state["start_date"] is None and charter_targets is not None:
            state["start_date"] = voo_snap.date
            state["bench_units"] = config.INITIAL_CAPITAL_JPY / (voo_snap.close * usdjpy_mid)
            log_and_report(f"[3] 初回構築: start_date={state['start_date']} bench_units={state['bench_units']:.6f}")
        else:
            log_and_report("[3] 初回構築スキップ（既に開始済み、またはターゲット未記入）")

        # 4. 配当処理
        state = portfolio.apply_dividends(state, snapshots)
        state = portfolio.apply_benchmark_dividend(state, voo_snap)
        log_and_report("[4] 配当処理完了")

        # 200日線カウンタ更新
        if voo_technicals.above_200dma_today:
            state["above_200dma_streak"] += 1
            state["below_200dma_streak"] = 0
        elif voo_technicals.below_200dma_today:
            state["below_200dma_streak"] += 1
            state["above_200dma_streak"] = 0

        # 5. 売買判断のスキップ条件
        skip_reason = None
        if charter_targets is None:
            skip_reason = "ターゲット未記入"
        elif state["start_date"] is None:
            skip_reason = "start_dateがnull"
        elif state["last_processed_voo_date"] == voo_snap.date:
            skip_reason = "休場（VOO終値日付が前回処理日から進んでいない）"

        accepted_trades: list[dict] = []
        rejected_trades: list[dict] = []

        if skip_reason:
            log_and_report(f"[5] 売買判断スキップ: ホールド（{skip_reason}）")
        else:
            decision = get_trade_decision(charter_text, state, snapshots, voo_technicals, usdjpy_mid)
            decision_reason = decision.get("reason", "")
            proposed_trades = decision.get("trades", [])
            state["mode"] = decision.get("mode", state["mode"])
            log_and_report(
                f"[5] Sonnet判断: mode={state['mode']} trades={len(proposed_trades)}件 reason={decision_reason}"
            )

            if dry_run:
                log_and_report("[6] dry-runのため約定処理はスキップ")
            else:
                trade_date = voo_snap.date
                state, accepted_trades, rejected_trades = portfolio.execute_trades(
                    proposed_trades, state, snapshots, usdjpy_mid, charter_targets, trade_date,
                )
                for t in accepted_trades:
                    portfolio.append_trade_row(t)
                log_and_report(f"[6] 約定処理完了: 約定{len(accepted_trades)}件 拒否{len(rejected_trades)}件")
                for r in rejected_trades:
                    logger.warning("拒否された注文: %s", r)

        state["last_processed_voo_date"] = voo_snap.date

        # 7. 評価・記録
        nav_jpy = portfolio.compute_nav_jpy(state, snapshots, usdjpy_mid)
        bench_jpy = portfolio.compute_bench_nav_jpy(state, voo_snap.close, usdjpy_mid)
        diff_jpy = nav_jpy - bench_jpy
        cash_ratio = portfolio.compute_cash_ratio(state, usdjpy_mid, nav_jpy) if nav_jpy else 0.0
        log_and_report(
            f"[7] 評価額計算: NAV={nav_jpy:,.0f}円 ベンチマーク={bench_jpy:,.0f}円 差額={diff_jpy:,.0f}円"
        )

        if not dry_run:
            portfolio.append_history_row({
                "date": voo_snap.date,
                "nav_jpy": round(nav_jpy),
                "bench_jpy": round(bench_jpy),
                "diff_jpy": round(diff_jpy),
                "diff_pct": round(diff_jpy / bench_jpy * 100, 4) if bench_jpy else 0.0,
                "cash_ratio": round(cash_ratio, 4),
            })
            portfolio.save_portfolio(state)

        # 8. data.json再生成
        data = report.build_data_json(state, snapshots, usdjpy_mid, nav_jpy, bench_jpy, accepted_trades, now_jst)
        if not dry_run:
            report.save_data_json(data)
            log_and_report("[8] data.json更新・台帳保存完了")
            git_commit_and_push(now_jst, logger)
            log_and_report("[8] git commit & push完了")
        else:
            log_and_report("[8] dry-runのためdata.json保存・git push・台帳保存はスキップ")

        # 9. Telegram送信
        prev_month_line = None
        if now_jst.day == 1:
            prev_month_dt = now_jst.replace(day=1) - dt.timedelta(days=1)
            prev_month_key = prev_month_dt.strftime("%Y-%m")
            for m in data["monthly"]:
                if m["month"] == prev_month_key:
                    result_jp = "勝ち" if m["result"] == "win" else "負け"
                    diff = m["nav"] - m["bench"]
                    prev_month_line = (
                        f"{prev_month_dt.month}月戦績: {result_jp} "
                        f"{'+' if diff >= 0 else ''}{diff:,}円"
                    )
                    break

        message = report.build_telegram_message(data, accepted_trades, now_jst, prev_month_line)
        if dry_run:
            log_and_report("[9] dry-runのためTelegram送信はスキップ。送信予定文面:\n" + message)
        else:
            ok = report.send_telegram_message(message)
            log_and_report(f"[9] Telegram送信{'成功' if ok else '失敗'}")

        log_and_report("[10] 正常終了")

    except Exception as e:  # noqa: BLE001 - 実行中の例外は握りつぶさず記録・通知する
        logger.exception("daily_run実行中に例外が発生")
        error_line = f"⚠️ vs-sp500 実行エラー: {e}"
        report_lines.append(error_line)
        if not dry_run:
            try:
                report.send_telegram_message(error_line)
            except Exception:
                logger.exception("エラー通知のTelegram送信にも失敗")
        else:
            report_lines.append("(dry-runのためエラー時Telegram送信はスキップ)")

    return "\n".join(report_lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="vs-sp500 daily run")
    parser.add_argument("--dry-run", action="store_true", help="約定・push・Telegram送信を行わず全フローを検証する")
    args = parser.parse_args()
    output = run(dry_run=args.dry_run)
    print(output)


if __name__ == "__main__":
    main()
