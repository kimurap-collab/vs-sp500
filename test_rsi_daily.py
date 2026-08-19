"""rsi_daily.py の候補確定・凍結読み込み・run()統合の単体テスト（2026-08-19改修1）。

改修1の要旨:
  1-a. 市場が閉まっている時間にscreen_rsi_candidates()を1回叩き、frozen_candidates.jsonへ保存する
       （freeze_candidates）。発注・台帳変更は一切しない。
  1-b. run()は寄り前に確定済みの候補があればそれを使い、執行時にRSIを再判定しない
       （候補のRSIが閾値を超えていても買う）。ただし株価は執行時の最新値を取り直す。
  1-c. frozen_candidates.jsonが無い・12時間より古い場合は、その場でスクリーナーを叩いて
       売買を止めない。この場合はrsi_basis="live"として記録する。

実データ・moomoo接続は一切使わない。broker/rsi_ledgerの外部I/Oはすべてモックする。
実行: python3 -m unittest test_rsi_daily.py -v
"""
from __future__ import annotations

import datetime as dt
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import rsi_daily
from market import TickerSnapshot

FILLED = {"order_id": "1", "status": "FILLED_ALL", "filled_qty": 198, "avg_price": 151.0}


def _rsi_state(**overrides):
    base = {
        "start_date": "2026-08-14",
        "cash_usd": 100_000.0,
        "lots": [],
        "bench_units_rsi": 100.0,
        "last_processed_date": None,
        "pending_orders": [],
    }
    base.update(overrides)
    return base


def _voo_snap():
    return TickerSnapshot(ticker="VOO", close=700.0, date="2026-08-18")


# ---------------------------------------------------------------------------
# 1-a: freeze_candidates()
# ---------------------------------------------------------------------------

class TestFreezeCandidates(unittest.TestCase):
    def test_moomoo_unavailable_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "frozen_candidates.json"
            with patch.object(config, "RSI_FROZEN_CANDIDATES_PATH", fake_path), \
                 patch("rsi_daily.broker.is_available", return_value=False), \
                 patch("rsi_daily.screen_rsi_candidates") as mock_screen, \
                 patch("rsi_daily._sleep_seconds") as mock_sleep:
                result = rsi_daily.freeze_candidates()
            self.assertIsNone(result)
            mock_screen.assert_not_called()
            self.assertFalse(fake_path.exists())
            self.assertEqual(mock_sleep.call_count, 2)  # 3回試行・間に2回待つ（改修2）

    def test_market_closed_records_prev_close_basis(self):
        """市場が閉まっている時間に叩けばrsi_basis="prev_close"として保存されること。"""
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "rsi" / "frozen_candidates.json"
            with patch.object(config, "RSI_FROZEN_CANDIDATES_PATH", fake_path), \
                 patch.object(config, "RSI_LEDGER_DIR", fake_path.parent), \
                 patch("rsi_daily.broker.is_available", return_value=True), \
                 patch("rsi_daily.broker.is_market_open_us", return_value=False), \
                 patch("rsi_daily.screen_rsi_candidates", return_value=[
                     {"ticker": "AAPL", "rsi14": 25.7, "price": 150.0, "date": "2026-08-18"},
                 ]):
                result = rsi_daily.freeze_candidates()

            self.assertEqual(result["rsi_basis"], "prev_close")
            self.assertEqual(len(result["candidates"]), 1)
            saved = json.loads(fake_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["rsi_basis"], "prev_close")
            self.assertEqual(saved["candidates"][0]["ticker"], "AAPL")
            self.assertIn("generated_at", saved)

    def test_market_open_records_live_basis(self):
        """手動再実行等で場中に叩かれた場合はprev_closeと言い切らずliveとして記録すること。"""
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "frozen_candidates.json"
            with patch.object(config, "RSI_FROZEN_CANDIDATES_PATH", fake_path), \
                 patch.object(config, "RSI_LEDGER_DIR", fake_path.parent), \
                 patch("rsi_daily.broker.is_available", return_value=True), \
                 patch("rsi_daily.broker.is_market_open_us", return_value=True), \
                 patch("rsi_daily.screen_rsi_candidates", return_value=[
                     {"ticker": "AAPL", "rsi14": 25.7, "price": 150.0, "date": "2026-08-18"},
                 ]), \
                 patch("rsi_daily._sleep_seconds"):
                result = rsi_daily.freeze_candidates()
            self.assertEqual(result["rsi_basis"], "live")

    def test_market_state_unknown_records_live_basis(self):
        """moomoo問い合わせ失敗(None)時は前日終値と言い切れないためliveとして扱うこと。"""
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "frozen_candidates.json"
            with patch.object(config, "RSI_FROZEN_CANDIDATES_PATH", fake_path), \
                 patch.object(config, "RSI_LEDGER_DIR", fake_path.parent), \
                 patch("rsi_daily.broker.is_available", return_value=True), \
                 patch("rsi_daily.broker.is_market_open_us", return_value=None), \
                 patch("rsi_daily.screen_rsi_candidates", return_value=[
                     {"ticker": "AAPL", "rsi14": 25.7, "price": 150.0, "date": "2026-08-18"},
                 ]), \
                 patch("rsi_daily._sleep_seconds"):
                result = rsi_daily.freeze_candidates()
            self.assertEqual(result["rsi_basis"], "live")


# ---------------------------------------------------------------------------
# 改修2: freeze_candidates()のリトライ（OpenD停止・接続失敗を最大3回まで再試行）
# 改修3: 取得失敗(None)と該当0件([])を区別する（0件はリトライせず即座に確定させる）
# ---------------------------------------------------------------------------

class TestFreezeCandidatesRetry(unittest.TestCase):
    def test_connection_failure_retries_then_succeeds_on_third_attempt(self):
        """接続失敗(None)が2回続いても3回目で候補が取れればそれを採用すること。"""
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "frozen_candidates.json"
            with patch.object(config, "RSI_FROZEN_CANDIDATES_PATH", fake_path), \
                 patch.object(config, "RSI_LEDGER_DIR", fake_path.parent), \
                 patch("rsi_daily.broker.is_available", return_value=True), \
                 patch("rsi_daily.broker.is_market_open_us", return_value=False), \
                 patch("rsi_daily.screen_rsi_candidates", side_effect=[
                     None, None, [{"ticker": "AAPL", "rsi14": 25.7, "price": 150.0, "date": "2026-08-18"}],
                 ]) as mock_screen, \
                 patch("rsi_daily._sleep_seconds") as mock_sleep:
                result = rsi_daily.freeze_candidates()

            self.assertEqual(mock_screen.call_count, 3)
            self.assertEqual(mock_sleep.call_args_list, [((60.0,),), ((300.0,),)])
            self.assertEqual(len(result["candidates"]), 1)
            self.assertTrue(fake_path.exists())

    def test_moomoo_unavailable_retries_then_recovers(self):
        """OpenD停止(is_available=False)が2回続いても3回目で復旧すれば採用すること。"""
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "frozen_candidates.json"
            with patch.object(config, "RSI_FROZEN_CANDIDATES_PATH", fake_path), \
                 patch.object(config, "RSI_LEDGER_DIR", fake_path.parent), \
                 patch("rsi_daily.broker.is_available", side_effect=[False, False, True]) as mock_avail, \
                 patch("rsi_daily.broker.is_market_open_us", return_value=False), \
                 patch("rsi_daily.screen_rsi_candidates", return_value=[
                     {"ticker": "MSFT", "rsi14": 20.0, "price": 300.0, "date": "2026-08-18"},
                 ]) as mock_screen, \
                 patch("rsi_daily._sleep_seconds") as mock_sleep:
                result = rsi_daily.freeze_candidates()

            self.assertEqual(mock_avail.call_count, 3)
            mock_screen.assert_called_once()  # is_available=Falseの間はscreenを叩かない
            self.assertEqual(mock_sleep.call_count, 2)
            self.assertEqual(len(result["candidates"]), 1)

    def test_all_three_attempts_fail_returns_none_and_writes_nothing(self):
        """3回とも接続に失敗(None)すればNoneを返しファイルを書かない（Telegram通知はしない）。"""
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "frozen_candidates.json"
            with patch.object(config, "RSI_FROZEN_CANDIDATES_PATH", fake_path), \
                 patch.object(config, "RSI_LEDGER_DIR", fake_path.parent), \
                 patch("rsi_daily.broker.is_available", return_value=True), \
                 patch("rsi_daily.screen_rsi_candidates", return_value=None) as mock_screen, \
                 patch("rsi_daily._sleep_seconds") as mock_sleep:
                result = rsi_daily.freeze_candidates()

            self.assertIsNone(result)
            self.assertEqual(mock_screen.call_count, 3)
            self.assertEqual(mock_sleep.call_count, 2)
            self.assertFalse(fake_path.exists())

    def test_zero_candidates_is_success_no_retry_writes_immediately(self):
        """該当0件(取得は成功=[])はリトライせず、待機ゼロで即座に空の候補ファイルを書くこと
        （2026-08-19改修3の本題。RSI32以下が本当に0件の強い相場日にNoneと区別できず
        6分間の無駄なリトライ待機の末にファイル未更新となっていたバグの修正）。"""
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "frozen_candidates.json"
            with patch.object(config, "RSI_FROZEN_CANDIDATES_PATH", fake_path), \
                 patch.object(config, "RSI_LEDGER_DIR", fake_path.parent), \
                 patch("rsi_daily.broker.is_available", return_value=True), \
                 patch("rsi_daily.broker.is_market_open_us", return_value=False), \
                 patch("rsi_daily.screen_rsi_candidates", return_value=[]) as mock_screen, \
                 patch("rsi_daily._sleep_seconds") as mock_sleep:
                start = time.monotonic()
                result = rsi_daily.freeze_candidates()
                elapsed = time.monotonic() - start

            mock_screen.assert_called_once()  # リトライせず1回で確定
            mock_sleep.assert_not_called()  # 待機ゼロ（_sleep_secondsが一切呼ばれない）
            self.assertLess(elapsed, 1.0)  # 実時間でも待機が発生していないことを示す
            self.assertIsNotNone(result)
            self.assertEqual(result["candidates"], [])
            saved = json.loads(fake_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["candidates"], [])


# ---------------------------------------------------------------------------
# 1-b/1-c: get_rsi_candidates()
# ---------------------------------------------------------------------------

class TestGetRsiCandidates(unittest.TestCase):
    def test_fresh_file_used_without_live_screening(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "frozen_candidates.json"
            fake_path.write_text(json.dumps({
                "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "rsi_basis": "prev_close",
                "candidates": [{"ticker": "AAPL", "rsi14": 25.7, "price": 150.0, "date": "2026-08-18"}],
            }), encoding="utf-8")
            with patch.object(config, "RSI_FROZEN_CANDIDATES_PATH", fake_path), \
                 patch("rsi_daily.screen_rsi_candidates") as mock_screen:
                candidates, basis = rsi_daily.get_rsi_candidates()
            mock_screen.assert_not_called()
            self.assertEqual(basis, "prev_close")
            self.assertEqual(candidates[0]["ticker"], "AAPL")

    def test_missing_file_falls_back_to_live_screening(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "frozen_candidates.json"  # 作らない＝存在しない
            with patch.object(config, "RSI_FROZEN_CANDIDATES_PATH", fake_path), \
                 patch("rsi_daily.screen_rsi_candidates", return_value=[
                     {"ticker": "MSFT", "rsi14": 28.0, "price": 300.0, "date": "2026-08-19"},
                 ]) as mock_screen:
                candidates, basis = rsi_daily.get_rsi_candidates()
            mock_screen.assert_called_once()
            self.assertEqual(basis, "live")
            self.assertEqual(candidates[0]["ticker"], "MSFT")

    def test_fresh_empty_file_used_without_live_screening(self):
        """空の候補ファイル(該当0件で確定されたもの)は「買う銘柄なし」の正常な結果として
        そのまま使い、場中の再取得(rsi_basis="live")に落ちないこと（改修3・検証c）。"""
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "frozen_candidates.json"
            fake_path.write_text(json.dumps({
                "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "rsi_basis": "prev_close",
                "candidates": [],
            }), encoding="utf-8")
            with patch.object(config, "RSI_FROZEN_CANDIDATES_PATH", fake_path), \
                 patch("rsi_daily.screen_rsi_candidates") as mock_screen:
                candidates, basis = rsi_daily.get_rsi_candidates()
            mock_screen.assert_not_called()  # ファイルが無い扱いにして再取得に落ちていないこと
            self.assertEqual(basis, "prev_close")  # "live"に化けていないこと
            self.assertEqual(candidates, [])

    def test_stale_file_falls_back_to_live_screening(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "frozen_candidates.json"
            stale = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=13)
            fake_path.write_text(json.dumps({
                "generated_at": stale.isoformat(),
                "rsi_basis": "prev_close",
                "candidates": [{"ticker": "AAPL", "rsi14": 25.7, "price": 150.0, "date": "2026-08-18"}],
            }), encoding="utf-8")
            with patch.object(config, "RSI_FROZEN_CANDIDATES_PATH", fake_path), \
                 patch("rsi_daily.screen_rsi_candidates", return_value=[]) as mock_screen:
                candidates, basis = rsi_daily.get_rsi_candidates()
            mock_screen.assert_called_once()
            self.assertEqual(basis, "live")


# ---------------------------------------------------------------------------
# 1-b/1-c: run()統合 — 確定済み候補は執行時に再判定せず買うこと／欠落時は自動復旧すること
# ---------------------------------------------------------------------------

class TestRunUsesFrozenCandidatesWithoutReassessingRsi(unittest.TestCase):
    def test_rsi_above_threshold_in_frozen_file_still_buys(self):
        """SPEC: 確定済み候補にRSI35(閾値32超)の銘柄が入っていても、執行時に再判定せず買うこと。"""
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "frozen_candidates.json"
            fake_path.write_text(json.dumps({
                "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "rsi_basis": "prev_close",
                "candidates": [{"ticker": "AAPL", "rsi14": 35.0, "price": 150.0, "date": "2026-08-18"}],
            }), encoding="utf-8")
            state = _rsi_state()

            with patch.object(config, "RSI_FROZEN_CANDIDATES_PATH", fake_path), \
                 patch("rsi_daily.screen_rsi_candidates") as mock_screen, \
                 patch("rsi_daily.fetch_market_data", return_value={
                     "AAPL": {"close": 151.0, "date": "2026-08-19"},
                 }) as mock_fetch, \
                 patch("rsi_daily.broker.get_cash", side_effect=[100_000.0, 100_000.0 - 198 * 151.0]), \
                 patch("rsi_daily.broker.place_market_order", return_value=FILLED), \
                 patch("rsi_daily.rsi_ledger.append_trade_row"), \
                 patch("rsi_daily.rsi_ledger.append_history_row"), \
                 patch("rsi_daily.rsi_ledger.save_portfolio"):
                new_state, accepted, log_lines, nav, bench, held = rsi_daily.run(
                    state, _voo_snap(), can_trade=True, already_processed_today=False,
                    dry_run=False, trade_date="2026-08-19", market_us="AFTERNOON",
                )

            mock_screen.assert_not_called()  # frozenがあるので執行時にスクリーナーを叩き直さない
            mock_fetch.assert_called_once_with(["AAPL"])  # 価格だけは執行時の最新値を取り直す
            self.assertEqual(len(accepted), 1)
            entry = accepted[0]
            self.assertEqual(entry["ticker"], "AAPL")
            self.assertEqual(entry["action"], "BUY")
            self.assertIn("RSI14=35.0", entry["note"])
            self.assertIn("basis=prev_close", entry["note"])
            self.assertEqual(len(new_state["lots"]), 1)


class TestRunFallsBackToLiveWhenFrozenMissing(unittest.TestCase):
    def test_missing_frozen_file_still_trades_with_live_basis(self):
        """SPEC: frozen_candidates.jsonが無い場合、その場で候補を作りrsi_basis="live"として
        記録し、売買が止まらないこと。"""
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "frozen_candidates.json"  # 存在しない
            state = _rsi_state()

            with patch.object(config, "RSI_FROZEN_CANDIDATES_PATH", fake_path), \
                 patch("rsi_daily.screen_rsi_candidates", return_value=[
                     {"ticker": "MSFT", "rsi14": 28.0, "price": 300.0, "date": "2026-08-19"},
                 ]) as mock_screen, \
                 patch("rsi_daily.fetch_market_data", return_value={}) as mock_fetch, \
                 patch("rsi_daily.broker.get_cash", side_effect=[100_000.0, 100_000.0 - 100 * 300.0]), \
                 patch("rsi_daily.broker.place_market_order", return_value={
                     "order_id": "2", "status": "FILLED_ALL", "filled_qty": 100, "avg_price": 300.0,
                 }), \
                 patch("rsi_daily.rsi_ledger.append_trade_row"), \
                 patch("rsi_daily.rsi_ledger.append_history_row"), \
                 patch("rsi_daily.rsi_ledger.save_portfolio"):
                new_state, accepted, log_lines, nav, bench, held = rsi_daily.run(
                    state, _voo_snap(), can_trade=True, already_processed_today=False,
                    dry_run=False, trade_date="2026-08-19", market_us="AFTERNOON",
                )

            mock_screen.assert_called_once()  # その場でスクリーナーを叩いた
            mock_fetch.assert_not_called()  # live候補の価格はscreen結果をそのまま使う(二重取得しない)
            self.assertEqual(len(accepted), 1)
            entry = accepted[0]
            self.assertEqual(entry["ticker"], "MSFT")
            self.assertIn("basis=live", entry["note"])
            self.assertTrue(any("basis=live" in line for line in log_lines))


if __name__ == "__main__":
    unittest.main()
