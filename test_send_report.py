"""send_report.py の自動復旧ロジック(2026-08-19改修2-b)の単体テスト。

前夜のループ日誌(logs/loop/*.md)が無い・古い場合にloop_run.shを起動すること、
ループが既に実行中なら二重起動しないことを検証する。
実際のclaude -p / moomoo / Telegram送信は一切行わない（全てモック）。
実行: python3 -m unittest test_send_report.py -v
"""
from __future__ import annotations

import datetime as dt
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import config
import send_report


class TestLoopJournalMissing(unittest.TestCase):
    def test_no_md_files_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            (log_dir / "loop").mkdir()
            with patch.object(config, "LOG_DIR", log_dir):
                self.assertTrue(send_report.loop_journal_missing(logging.getLogger("t")))

    def test_fresh_md_file_is_not_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            loop_dir = log_dir / "loop"
            loop_dir.mkdir()
            (loop_dir / "2026-08-19.md").write_text("日誌", encoding="utf-8")
            with patch.object(config, "LOG_DIR", log_dir):
                self.assertFalse(send_report.loop_journal_missing(logging.getLogger("t")))

    def test_stale_md_file_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            loop_dir = log_dir / "loop"
            loop_dir.mkdir()
            stale_file = loop_dir / "2026-08-17.md"
            stale_file.write_text("日誌", encoding="utf-8")
            old_time = (dt.datetime.now() - dt.timedelta(hours=21)).timestamp()
            import os
            os.utime(stale_file, (old_time, old_time))
            with patch.object(config, "LOG_DIR", log_dir):
                self.assertTrue(send_report.loop_journal_missing(logging.getLogger("t")))


class TestRestartLoopIfMissing(unittest.TestCase):
    def test_missing_journal_and_not_running_starts_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            (log_dir / "loop").mkdir()
            with patch.object(config, "LOG_DIR", log_dir), \
                 patch("send_report.loop_already_running", return_value=False), \
                 patch("send_report.subprocess.Popen") as mock_popen:
                send_report.restart_loop_if_missing(logging.getLogger("t"))
            mock_popen.assert_called_once()
            args, kwargs = mock_popen.call_args
            self.assertIn("loop_run.sh", args[0][1])
            self.assertTrue(kwargs.get("start_new_session"))

    def test_missing_journal_but_already_running_does_not_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            (log_dir / "loop").mkdir()
            with patch.object(config, "LOG_DIR", log_dir), \
                 patch("send_report.loop_already_running", return_value=True), \
                 patch("send_report.subprocess.Popen") as mock_popen:
                send_report.restart_loop_if_missing(logging.getLogger("t"))
            mock_popen.assert_not_called()

    def test_fresh_journal_does_not_start_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            loop_dir = log_dir / "loop"
            loop_dir.mkdir()
            (loop_dir / "2026-08-19.md").write_text("日誌", encoding="utf-8")
            with patch.object(config, "LOG_DIR", log_dir), \
                 patch("send_report.loop_already_running") as mock_running, \
                 patch("send_report.subprocess.Popen") as mock_popen:
                send_report.restart_loop_if_missing(logging.getLogger("t"))
            mock_running.assert_not_called()  # 日誌が新しければ実行中かどうかすら見に行かない
            mock_popen.assert_not_called()


class TestLoopAlreadyRunning(unittest.TestCase):
    def test_pgrep_found_means_running(self):
        with patch("send_report.subprocess.run", return_value=MagicMock(returncode=0)):
            self.assertTrue(send_report.loop_already_running())

    def test_pgrep_not_found_means_not_running(self):
        with patch("send_report.subprocess.run", return_value=MagicMock(returncode=1)):
            self.assertFalse(send_report.loop_already_running())


class TestNoPendingMessage(unittest.TestCase):
    """2026-09-03修正: moomoo未接続時に原因(OpenD停止)と対処を報告文に書く。"""

    def test_moomoo_unavailable_gives_actionable_fix(self):
        msg = send_report.build_no_pending_message(moomoo_available=False)
        self.assertIn("OpenD", msg)
        self.assertIn("繋がっていない", msg)

    def test_moomoo_available_blames_the_night_job_instead(self):
        msg = send_report.build_no_pending_message(moomoo_available=True)
        self.assertIn("接続できている", msg)
        self.assertNotIn("OpenD.app を起動", msg)


class TestMainChecksMoomooBeforeWarning(unittest.TestCase):
    """main()が読み取り不能時にbroker.is_availableを呼び、結果で文面を分けることの確認。

    実際のmoomoo・Telegram送信・自律ループ起動には一切触れない（全てモック）。
    """

    def test_no_pending_and_moomoo_down_sends_opend_instruction(self):
        with patch("send_report.setup_logging", return_value=logging.getLogger("t")), \
             patch("send_report.read_pending", return_value=None), \
             patch("send_report.broker.is_available", return_value=False), \
             patch("send_report.report.send_telegram_message", return_value=True) as mock_send, \
             patch("send_report.restart_loop_if_missing"):
            with self.assertRaises(SystemExit):
                send_report.main()
        sent_text = mock_send.call_args[0][0]
        self.assertIn("OpenD", sent_text)

    def test_no_pending_and_moomoo_up_sends_generic_warning(self):
        with patch("send_report.setup_logging", return_value=logging.getLogger("t")), \
             patch("send_report.read_pending", return_value=None), \
             patch("send_report.broker.is_available", return_value=True), \
             patch("send_report.report.send_telegram_message", return_value=True) as mock_send, \
             patch("send_report.restart_loop_if_missing"):
            with self.assertRaises(SystemExit):
                send_report.main()
        sent_text = mock_send.call_args[0][0]
        self.assertIn("接続できている", sent_text)
        self.assertNotIn("OpenD.app を起動", sent_text)


if __name__ == "__main__":
    unittest.main()
