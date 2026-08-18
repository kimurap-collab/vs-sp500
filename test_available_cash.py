"""2026-08-18 修正1: 未決の買い注文の代金を現金から予約する、の単体テスト。

新規注文の可否判断・数量計算に使う現金 = 台帳のcash_usd − 自枠の未決BUYの額面合計
（未約定残数×発注時点の想定単価est_price）。一部約定している場合は残数分だけ予約する。
本体はportfolio.compute_available_cash、RSI枠はrsi_ledger.compute_available_cashで
それぞれ完結し、互いの枠のpending_ordersは見ない。

実行: python3 -m unittest test_available_cash.py -v
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import portfolio
import rsi_ledger
from market import TickerSnapshot


def _main_state(**overrides):
    base = {
        "start_date": "2026-08-05", "mode": "normal", "cash_usd": 10000.0,
        "holdings": {}, "bench_units": 10.0, "last_processed_voo_date": None,
        "below_200dma_streak": 0, "above_200dma_streak": 0, "pending_orders": [],
    }
    base.update(overrides)
    return base


def _rsi_state(**overrides):
    base = {
        "start_date": "2026-08-14", "cash_usd": 500000.0, "lots": [],
        "bench_units_rsi": 700.0, "last_processed_date": "2026-08-17", "pending_orders": [],
    }
    base.update(overrides)
    return base


def _snap(ticker, close, date="2026-08-18"):
    return TickerSnapshot(ticker=ticker, close=close, date=date)


# ---------------------------------------------------------------------------
# portfolio.compute_available_cash（本体）
# ---------------------------------------------------------------------------

class TestMainComputeAvailableCash(unittest.TestCase):
    def test_no_pending_orders_returns_full_cash(self):
        state = _main_state(cash_usd=10000.0, pending_orders=[])
        self.assertEqual(portfolio.compute_available_cash(state), 10000.0)

    def test_unfilled_buy_reserves_full_face_value(self):
        state = _main_state(cash_usd=10000.0, pending_orders=[{
            "order_id": "1", "ticker": "VOO", "side": "BUY", "qty": 10,
            "applied_qty": 0, "est_price": 100.0,
        }])
        # 10株 × $100 = $1,000 を予約 → 発注可能額は$9,000
        self.assertAlmostEqual(portfolio.compute_available_cash(state), 9000.0)

    def test_partially_filled_buy_reserves_only_remaining_qty(self):
        state = _main_state(cash_usd=10000.0, pending_orders=[{
            "order_id": "1", "ticker": "VOO", "side": "BUY", "qty": 10,
            "applied_qty": 4, "est_price": 100.0,
        }])
        # 残数6株だけ予約 → $600
        self.assertAlmostEqual(portfolio.compute_available_cash(state), 9400.0)

    def test_sell_orders_are_not_reserved(self):
        state = _main_state(cash_usd=10000.0, pending_orders=[{
            "order_id": "1", "ticker": "VOO", "side": "SELL", "qty": 10,
            "applied_qty": 0, "est_price": 100.0,
        }])
        self.assertEqual(portfolio.compute_available_cash(state), 10000.0)

    def test_fully_applied_buy_reserves_nothing(self):
        state = _main_state(cash_usd=10000.0, pending_orders=[{
            "order_id": "1", "ticker": "VOO", "side": "BUY", "qty": 10,
            "applied_qty": 10, "est_price": 100.0,
        }])
        self.assertEqual(portfolio.compute_available_cash(state), 10000.0)

    def test_missing_est_price_falls_back_to_market_price(self):
        """この修正より前に発注された旧pending（est_price未記録）は現在値で代用する。"""
        state = _main_state(cash_usd=10000.0, pending_orders=[{
            "order_id": "3145613", "ticker": "EWJ", "side": "BUY", "qty": 65, "applied_qty": 0,
        }])
        available = portfolio.compute_available_cash(state, market_prices={"EWJ": 30.0})
        self.assertAlmostEqual(available, 10000.0 - 65 * 30.0)

    def test_missing_est_price_and_no_market_price_reserves_zero_with_warning(self):
        state = _main_state(cash_usd=10000.0, pending_orders=[{
            "order_id": "3145613", "ticker": "EWJ", "side": "BUY", "qty": 65, "applied_qty": 0,
        }])
        with self.assertLogs("vs-sp500.portfolio", level="WARNING"):
            available = portfolio.compute_available_cash(state, market_prices={})
        self.assertEqual(available, 10000.0)

    def test_multiple_buy_orders_sum_reservations(self):
        state = _main_state(cash_usd=10000.0, pending_orders=[
            {"order_id": "1", "ticker": "VOO", "side": "BUY", "qty": 10, "applied_qty": 0, "est_price": 100.0},
            {"order_id": "2", "ticker": "QQQ", "side": "BUY", "qty": 5, "applied_qty": 2, "est_price": 200.0},
        ])
        # VOO残10株×100=1000 + QQQ残3株×200=600 → 予約1600
        self.assertAlmostEqual(portfolio.compute_available_cash(state), 10000.0 - 1600.0)


# ---------------------------------------------------------------------------
# rsi_ledger.compute_available_cash（RSI枠）
# ---------------------------------------------------------------------------

class TestRsiComputeAvailableCash(unittest.TestCase):
    def test_unfilled_buy_reserves_face_value(self):
        state = _rsi_state(cash_usd=500000.0, pending_orders=[{
            "order_id": "9", "ticker": "AAPL", "side": "BUY", "qty": 100,
            "applied_qty": 0, "est_price": 150.0,
        }])
        self.assertAlmostEqual(rsi_ledger.compute_available_cash(state), 500000.0 - 15000.0)

    def test_partial_fill_reserves_remainder_only(self):
        state = _rsi_state(cash_usd=500000.0, pending_orders=[{
            "order_id": "9", "ticker": "AAPL", "side": "BUY", "qty": 100,
            "applied_qty": 60, "est_price": 150.0,
        }])
        self.assertAlmostEqual(rsi_ledger.compute_available_cash(state), 500000.0 - 40 * 150.0)

    def test_main_book_pending_orders_are_never_seen(self):
        """RSI枠のcompute_available_cashは自枠のpending_ordersしか見ない（本体は別引数のため触れようがない）。"""
        rsi_state = _rsi_state(cash_usd=500000.0, pending_orders=[])
        self.assertEqual(rsi_ledger.compute_available_cash(rsi_state), 500000.0)


# ---------------------------------------------------------------------------
# execute_trades統合: 未決BUYがある状態で新規注文の可否判断に反映されること
# ---------------------------------------------------------------------------

class TestExecuteTradesRespectsAvailableCash(unittest.TestCase):
    def test_buy_rejected_when_exceeding_available_cash_despite_raw_cash_sufficient(self):
        # cash_usd=10000だが、既存の未決BUY(VOO 90株@$100=$9,000)がほぼ全額を予約しているため
        # 発注可能額は$1,000しか無い。新規のQQQ $1,500 BUYはraw cash_usdなら通るが拒否されるべき。
        # rule="rebalance"（ターゲット系）にして、非ターゲット取引特有の日次上限チェックを
        # 迂回し、修正1の現金予約チェックだけを単体で検証する。
        state = _main_state(cash_usd=10000.0, holdings={}, pending_orders=[{
            "order_id": "1", "ticker": "VOO", "side": "BUY", "qty": 90,
            "applied_qty": 0, "est_price": 100.0, "submitted_date": "2026-08-17", "rule": "rebalance",
        }])
        market = {"VOO": _snap("VOO", 100.0), "QQQ": _snap("QQQ", 100.0)}
        trade = {"action": "BUY", "ticker": "QQQ", "amount_usd": 1500.0, "rule": "rebalance"}
        with patch("portfolio.broker.get_cash", return_value=10000.0), \
             patch("portfolio.broker.place_market_order") as mock_place:
            new_state, accepted, rejected, queued = portfolio.execute_trades(
                [trade], state, market, None, "2026-08-18",
            )
        self.assertEqual(accepted, [])
        self.assertEqual(len(rejected), 1)
        self.assertIn("現金不足", rejected[0]["reason"])
        mock_place.assert_not_called()  # ガードレールで弾かれ、実発注まで到達しない

    def test_buy_accepted_when_within_available_cash(self):
        state = _main_state(cash_usd=10000.0, holdings={}, pending_orders=[{
            "order_id": "1", "ticker": "VOO", "side": "BUY", "qty": 90,
            "applied_qty": 0, "est_price": 100.0, "submitted_date": "2026-08-17", "rule": "rebalance",
        }])
        # 発注可能額は$1,000。$500のQQQ BUYなら収まる。
        market = {"VOO": _snap("VOO", 100.0), "QQQ": _snap("QQQ", 100.0)}
        trade = {"action": "BUY", "ticker": "QQQ", "amount_usd": 500.0, "rule": "rebalance"}
        with patch("portfolio.broker.get_cash", return_value=10000.0), \
             patch("portfolio.broker.place_market_order", return_value={
                 "order_id": "99", "status": "FILLED_ALL", "filled_qty": 5, "avg_price": 100.0,
             }):
            new_state, accepted, rejected, queued = portfolio.execute_trades(
                [trade], state, market, None, "2026-08-18",
            )
        self.assertEqual(rejected, [])
        self.assertEqual(len(accepted), 1)


if __name__ == "__main__":
    unittest.main()
