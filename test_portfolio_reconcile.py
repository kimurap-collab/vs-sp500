"""portfolio.reconcile_positions の単体テスト（本体+RSI枠の合算照合）。

SPEC_RSI30.md「最大の危険箇所は保有照合」の検証4に対応する。
brokerは実接続せず、broker.get_positionsをモックして判定ロジックだけを検証する。
実行: python3 -m unittest test_portfolio_reconcile.py -v
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import portfolio


def _main_state(holdings):
    return {"holdings": holdings}


def _rsi_state(lots):
    return {"lots": lots}


def _open_lot(ticker, shares):
    return {"ticker": ticker, "shares": shares, "closed": False}


def _closed_lot(ticker, shares):
    return {"ticker": ticker, "shares": 0, "closed": True}


class TestCombinedHoldings(unittest.TestCase):
    def test_rsi_state_none_equals_main_only(self):
        main = _main_state({"VOO": 31.0, "QQQ": 21.0})
        self.assertEqual(portfolio.combined_holdings(main, None), {"VOO": 31.0, "QQQ": 21.0})

    def test_rsi_empty_lots_equals_main_only(self):
        main = _main_state({"VOO": 31.0})
        rsi = _rsi_state([])
        self.assertEqual(portfolio.combined_holdings(main, rsi), {"VOO": 31.0})

    def test_sums_by_ticker_across_arms(self):
        main = _main_state({"VOO": 31.0, "QQQ": 21.0})
        rsi = _rsi_state([_open_lot("AAPL", 100), _open_lot("QQQ", 5)])
        combined = portfolio.combined_holdings(main, rsi)
        self.assertEqual(combined, {"VOO": 31.0, "QQQ": 26.0, "AAPL": 100})

    def test_multiple_lots_same_ticker_sum_together(self):
        main = _main_state({})
        rsi = _rsi_state([_open_lot("NVDA", 10), _open_lot("NVDA", 7)])
        self.assertEqual(portfolio.combined_holdings(main, rsi), {"NVDA": 17})

    def test_closed_lots_excluded(self):
        main = _main_state({})
        rsi = _rsi_state([_open_lot("NVDA", 10), _closed_lot("NVDA", 0)])
        self.assertEqual(portfolio.combined_holdings(main, rsi), {"NVDA": 10})


class TestReconcilePositions(unittest.TestCase):
    def test_reconciles_ok_when_rsi_empty_and_main_matches_broker(self):
        main = _main_state({"VOO": 31.0, "QQQ": 21.0})
        rsi = _rsi_state([])
        with patch("portfolio.broker.get_positions", return_value={"VOO": 31.0, "QQQ": 21.0}):
            ok, msg = portfolio.reconcile_positions(main, rsi)
        self.assertTrue(ok)

    def test_reconciles_ok_when_rsi_positions_present_and_summed_matches_broker(self):
        main = _main_state({"VOO": 31.0})
        rsi = _rsi_state([_open_lot("AAPL", 50)])
        with patch("portfolio.broker.get_positions", return_value={"VOO": 31.0, "AAPL": 50.0}):
            ok, msg = portfolio.reconcile_positions(main, rsi)
        self.assertTrue(ok)

    def test_fails_when_rsi_lot_not_reflected_in_broker(self):
        main = _main_state({"VOO": 31.0})
        rsi = _rsi_state([_open_lot("AAPL", 50)])
        with patch("portfolio.broker.get_positions", return_value={"VOO": 31.0}):  # AAPLが無い
            ok, msg = portfolio.reconcile_positions(main, rsi)
        self.assertFalse(ok)

    def test_fails_when_broker_has_extra_shares_not_in_either_ledger(self):
        main = _main_state({"VOO": 31.0})
        rsi = _rsi_state([_open_lot("AAPL", 50)])
        with patch("portfolio.broker.get_positions", return_value={"VOO": 31.0, "AAPL": 60.0}):
            ok, msg = portfolio.reconcile_positions(main, rsi)
        self.assertFalse(ok)

    def test_reconciles_ok_when_no_rsi_state_passed_backward_compat(self):
        main = _main_state({"VOO": 31.0})
        with patch("portfolio.broker.get_positions", return_value={"VOO": 31.0}):
            ok, msg = portfolio.reconcile_positions(main)
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
