#!/usr/bin/env python3
"""vs-sp500: 朝07:00にTelegram報告を送るだけのジョブ。

夜23:00（米国市場の場中）の実行が pending_report.txt に貯めた文面をまとめて送る。
計算も売買も一切しない。夜の記録が見つからない場合は、その事実を警告として送る。
"""
from __future__ import annotations

import datetime as dt
import logging
import os
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
        sys.exit(0)

    ok = report.send_telegram_message(text)
    if ok:
        config.PENDING_REPORT_PATH.write_text("", encoding="utf-8")  # 送信済みは空にする
        logger.info("保留報告を送信して空にした（%d文字）", len(text))
    else:
        logger.error("送信に失敗したため保留ファイルは残す（次回再送される）")
    print(("送信成功" if ok else "送信失敗") + f": {len(text)}文字")


if __name__ == "__main__":
    main()
