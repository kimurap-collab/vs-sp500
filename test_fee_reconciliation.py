"""2026-08-18 修正3: 手数料を口座の実残高の増減から逆算して計上する、の単体テスト。

moomoo APIには手数料そのものを返す経路が無いため、実行の最後に記録した口座全体の現金残高
（ledger/account_state.json）と、次回決済時点の現金残高との差分から、今回決済した全注文
（本体・RSI枠合算）の額面合計を差し引いて手数料を逆算する。

実行: python3 -m unittest test_fee_reconciliation.py -v
"""
from __future__ import annotations

import unittest

import account_state


class TestReconcileFeesFirstRun(unittest.TestCase):
    def test_no_previous_checkpoint_skips_reconciliation(self):
        fees, logs = account_state.reconcile_fees(
            None, 100000.0, {"main": [{"action": "BUY", "amount_usd": 500.0}], "rsi": []},
        )
        self.assertEqual(fees, {"main": 0.0, "rsi": 0.0})
        self.assertTrue(any("初回" in line for line in logs))


class TestReconcileFeesNoSettlement(unittest.TestCase):
    def test_no_trades_settled_but_cash_moved_logs_only(self):
        # 決済0件なのに口座現金が+50動いた（配当・手動売買の可能性）→ 手数料としては計上しない
        fees, logs = account_state.reconcile_fees(100000.0, 100050.0, {"main": [], "rsi": []})
        self.assertEqual(fees, {"main": 0.0, "rsi": 0.0})
        self.assertTrue(any("配当" in line for line in logs))

    def test_no_trades_no_cash_movement_is_silent(self):
        fees, logs = account_state.reconcile_fees(100000.0, 100000.0, {"main": [], "rsi": []})
        self.assertEqual(fees, {"main": 0.0, "rsi": 0.0})
        self.assertEqual(logs, [])


class TestReconcileFeesSingleBuy(unittest.TestCase):
    def test_buy_fee_computed_correctly(self):
        # BUY $100の注文1件。口座現金は$101減った（$100の代金＋$1の手数料）
        fees, logs = account_state.reconcile_fees(
            10000.0, 10000.0 - 101.0,
            {"main": [{"action": "BUY", "amount_usd": 100.0}], "rsi": []},
        )
        self.assertAlmostEqual(fees["main"], 1.0, places=6)
        self.assertAlmostEqual(fees["rsi"], 0.0, places=6)


class TestReconcileFeesSingleSell(unittest.TestCase):
    def test_sell_fee_computed_correctly(self):
        # SELL $100の注文1件。口座現金は$99増えた（$100の代金－$1の手数料）
        fees, logs = account_state.reconcile_fees(
            10000.0, 10000.0 + 99.0,
            {"main": [{"action": "SELL", "amount_usd": 100.0}], "rsi": []},
        )
        self.assertAlmostEqual(fees["main"], 1.0, places=6)


class TestReconcileFeesApportionment(unittest.TestCase):
    def test_fee_apportioned_by_face_value_ratio_between_books(self):
        # 本体BUY $300、RSI枠BUY $700。合計$1000の取引に対し手数料$2（$1998の現金減）。
        # 額面比 3:7 で按分 → 本体$0.6・RSI$1.4
        fees, logs = account_state.reconcile_fees(
            10000.0, 10000.0 - 1002.0,
            {
                "main": [{"action": "BUY", "amount_usd": 300.0}],
                "rsi": [{"action": "BUY", "amount_usd": 700.0}],
            },
        )
        self.assertAlmostEqual(fees["main"], 0.6, places=6)
        self.assertAlmostEqual(fees["rsi"], 1.4, places=6)
        self.assertAlmostEqual(sum(fees.values()), 2.0, places=6)


class TestReconcileFeesMixedBuySell(unittest.TestCase):
    def test_mixed_buy_and_sell_in_same_round(self):
        # BUY $500・SELL $200が同じ回で決済。フェア（手数料無し）なら現金は-300動くはず。
        # 実際は-303動いた → 手数料$3
        fees, logs = account_state.reconcile_fees(
            10000.0, 10000.0 - 303.0,
            {
                "main": [
                    {"action": "BUY", "amount_usd": 500.0},
                    {"action": "SELL", "amount_usd": 200.0},
                ],
                "rsi": [],
            },
        )
        self.assertAlmostEqual(fees["main"], 3.0, places=6)


class TestReconcileFeesWarningThreshold(unittest.TestCase):
    def test_fee_over_20_dollars_warns(self):
        fees, logs = account_state.reconcile_fees(
            10000.0, 10000.0 - 125.0,
            {"main": [{"action": "BUY", "amount_usd": 100.0}], "rsi": []},
        )
        self.assertAlmostEqual(fees["main"], 25.0, places=6)
        self.assertTrue(any("$20超" in line for line in logs), logs)

    def test_fee_under_20_dollars_no_warning(self):
        fees, logs = account_state.reconcile_fees(
            10000.0, 10000.0 - 105.0,
            {"main": [{"action": "BUY", "amount_usd": 100.0}], "rsi": []},
        )
        self.assertAlmostEqual(fees["main"], 5.0, places=6)
        self.assertFalse(any("$20超" in line for line in logs), logs)


class TestReconcileFeesNegativeAnomaly(unittest.TestCase):
    def test_negative_fee_is_not_applied(self):
        # 額面通りぴったり、かつ現金がむしろ増えている（あり得ない＝異常）→ 計上しない
        fees, logs = account_state.reconcile_fees(
            10000.0, 10000.0 - 90.0,  # BUY $100のはずが$90しか減っていない
            {"main": [{"action": "BUY", "amount_usd": 100.0}], "rsi": []},
        )
        self.assertEqual(fees, {"main": 0.0, "rsi": 0.0})
        self.assertTrue(any("負値" in line for line in logs), logs)


class TestAccountStatePersistence(unittest.TestCase):
    def test_load_returns_none_when_file_missing(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "account_state.json"
            with patch.object(account_state, "ACCOUNT_STATE_PATH", fake_path):
                self.assertIsNone(account_state.load_account_cash())

    def test_save_then_load_roundtrip(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "ledger" / "account_state.json"
            with patch.object(account_state, "ACCOUNT_STATE_PATH", fake_path), \
                 patch.object(account_state.config, "LEDGER_DIR", fake_path.parent):
                account_state.save_account_cash(12345.67, "2026-08-18")
                self.assertAlmostEqual(account_state.load_account_cash(), 12345.67)


if __name__ == "__main__":
    unittest.main()
