#!/usr/bin/env python3
"""vs-sp500: 朝07:00にTelegram報告を送るだけのジョブ。

夜23:00（米国市場の場中）の実行が pending_report.txt に貯めた文面をまとめて送る。
計算も売買も一切しない。夜の記録が見つからない場合は、その事実を警告として送る。

2026-08-19改修2-b: 前夜の自律ループ日誌（logs/loop/*.md）が見当たらない場合、その場で
loop_run.shをバックグラウンド起動して自動復旧させる（大将への通知は追加しない）。
報告の送信は待たせない。ループが実行中なら二重起動しない。
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import subprocess
import sys

import config
import report

STALE_HOURS = 20  # 前回の夜間実行からこれ以上経っていたら「記録なし」とみなす


def setup_logging() -> logging.Logger:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("vs-sp500-report")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        fh = logging.FileHandler(config.LOG_DIR / "send_report.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(fh)
    return logger


def read_pending() -> str | None:
    """貯まっている報告を返す。無い・古い・空ならNone。"""
    path = config.PENDING_REPORT_PATH
    if not path.exists():
        return None
    age_hours = (dt.datetime.now().timestamp() - path.stat().st_mtime) / 3600
    if age_hours > STALE_HOURS:
        return None
    text = path.read_text(encoding="utf-8").strip()
    return text or None


def loop_journal_missing(logger: logging.Logger) -> bool:
    """前夜のループ日誌(logs/loop/*.md)が無い・古ければTrueを返す。

    ファイル名の日付規則（JST基準。2026-08-17改修で実行時刻が23:00→02:00近辺に変わった
    経緯もあり、直近ファイルの更新時刻で判定する方がファイル名の日付パースより頑丈）で
    最新のものを見て、pending_report.txtの鮮度判定（read_pending）と同じSTALE_HOURSを使う。
    """
    loop_dir = config.LOG_DIR / "loop"
    md_files = list(loop_dir.glob("*.md"))
    if not md_files:
        return True
    latest = max(md_files, key=lambda p: p.stat().st_mtime)
    age_hours = (dt.datetime.now().timestamp() - latest.stat().st_mtime) / 3600
    if age_hours > STALE_HOURS:
        logger.warning("最新のループ日誌(%s)が%.1f時間前と古い", latest.name, age_hours)
        return True
    return False


def loop_already_running() -> bool:
    """loop_run.shが既に実行中か（二重起動防止）。"""
    result = subprocess.run(["pgrep", "-f", "loop_run.sh"], capture_output=True)
    return result.returncode == 0


def restart_loop_if_missing(logger: logging.Logger) -> None:
    """前夜の日誌が無ければloop_run.shをバックグラウンドで起動する。報告の送信は待たせない。"""
    if not loop_journal_missing(logger):
        return
    if loop_already_running():
        logger.info("前夜の日誌が無いが、ループは既に実行中のため起動しない")
        return
    logger.warning("前夜のループ日誌が見つからないため、loop_run.shをその場で起動する")
    subprocess.Popen(
        ["/bin/bash", str(config.BASE_DIR / "loop_run.sh")],
        cwd=str(config.BASE_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def main() -> None:
    logger = setup_logging()
    # このジョブ自身は保留せず即送信する（夜間ジョブの環境変数を引き継がないよう明示的に外す）
    os.environ.pop("VS_SP500_DEFER_TELEGRAM", None)

    text = read_pending()
    if text is None:
        msg = (
            "⚠️ vs-sp500: 夜間実行の記録が見つからん。\n"
            f"（{config.PENDING_REPORT_PATH.name} が無い・空・{STALE_HOURS}時間以上古い）\n"
            "夜23:00のジョブが失敗しとる可能性がある。logs/launchd.err を確認してくれ。"
        )
        ok = report.send_telegram_message(msg)
        logger.warning("保留報告なし → 警告を送信: %s", "成功" if ok else "失敗")
        print(msg)
        restart_loop_if_missing(logger)
        sys.exit(0)

    ok = report.send_telegram_message(text)
    if ok:
        config.PENDING_REPORT_PATH.write_text("", encoding="utf-8")  # 送信済みは空にする
        logger.info("保留報告を送信して空にした（%d文字）", len(text))
    else:
        logger.error("送信に失敗したため保留ファイルは残す（次回再送される）")
    print(("送信成功" if ok else "送信失敗") + f": {len(text)}文字")
    restart_loop_if_missing(logger)


if __name__ == "__main__":
    main()
