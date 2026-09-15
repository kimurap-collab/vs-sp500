"""ターゲット系BUYが現金不足のとき、全額拒否ではなく現金下限内に株数を縮小して発注することの検証。

2026-09-16: VOO買い18株の見積りが発注可能額を$2超えて拒否され、現金19.9%が放置された事故の再発防止。
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import config
import portfolio
from market import TickerSnapshot


def _state(**overrides):
    base = {
        "start_date": "2026-08-05", "mode": "normal", "cash_usd": 10000.0,
        "holdings": {}, "bench_units": 10.0, "last_processed_voo_date": None,
        "below_200dma_streak": 0, "above_200dma_streak": 0, "pending_orders": [],
    }
    base.update(overrides)
    return base


def _snap(ticker, close, date="2026-09-15"):
    return TickerSnapshot(ticker=ticker, close=close, date=date)


class TestRebalanceBuyShrinksToCash(unittest.TestCase):
    def test_target_buy_exceeding_cash_is_shrunk_to_keep_cash_floor(self):
        # 現金$10,000・保有QQQ $40,000（NAV $50,000）。VOO $10,500 BUY（105株@$100）は
        # 現金を超えるので、現金下限2%（$1,000）を残した$9,000＝90株に縮小して発注される。
        state = _state(cash_usd=10000.0, holdings={"QQQ": 400.0})
        market = {"VOO": _snap("VOO", 100.0), "QQQ": _snap("QQQ", 100.0)}
        trade = {"action": "BUY", "ticker": "VOO", "amount_usd": 10500.0, "rule": "rebalance"}
        with patch("portfolio.broker.get_cash", side_effect=[10000.0, 1000.0]), \
             patch("portfolio.broker.place_market_order", return_value={
                 "order_id": "1", "status": "FILLED_ALL", "filled_qty": 90, "avg_price": 100.0,
             }) as mock_place:
            new_state, accepted, rejected, queued = portfolio.execute_trades(
                [trade], state, market, None, "2026-09-16",
            )
        self.assertEqual(rejected, [])
        self.assertEqual(len(accepted), 1)
        mock_place.assert_called_once_with("VOO", 90, "BUY")
        self.assertEqual(new_state["holdings"]["VOO"], 90)

    def test_target_buy_rejected_when_floor_leaves_no_share(self):
        # 現金$1,050・NAV $50,000 → 下限$1,000を残すと$50＝0株。拒否される。
        state = _state(cash_usd=1050.0, holdings={"QQQ": 489.5})
        market = {"VOO": _snap("VOO", 100.0), "QQQ": _snap("QQQ", 100.0)}
        trade = {"action": "BUY", "ticker": "VOO", "amount_usd": 1000.0, "rule": "rebalance"}
        with patch("portfolio.broker.get_cash", return_value=1050.0), \
             patch("portfolio.broker.place_market_order") as mock_place:
            _, accepted, rejected, _ = portfolio.execute_trades([trade], state, market, None, "2026-09-16")
        self.assertEqual(accepted, [])
        self.assertEqual(len(rejected), 1)
        self.assertIn("現金不足", rejected[0]["reason"])
        mock_place.assert_not_called()

    def test_non_target_buy_is_not_shrunk(self):
        # 押し目買い（非ターゲット）は縮小の対象外。既存の「現金の半分まで」で拒否される。
        state = _state(cash_usd=10000.0, holdings={"QQQ": 400.0})
        market = {"VOO": _snap("VOO", 100.0), "QQQ": _snap("QQQ", 100.0)}
        trade = {"action": "BUY", "ticker": "VOO", "amount_usd": 6000.0, "rule": "dip_buy"}
        with patch("portfolio.broker.get_cash", return_value=10000.0), \
             patch("portfolio.broker.place_market_order") as mock_place:
            _, accepted, rejected, _ = portfolio.execute_trades([trade], state, market, None, "2026-09-16")
        self.assertEqual(accepted, [])
        self.assertEqual(len(rejected), 1)
        mock_place.assert_not_called()


if __name__ == "__main__":
    unittest.main()
