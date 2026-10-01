"""ターゲット0%銘柄の強制全売却（charter.md 発動条件1・v1.8 2026-10-01）の検証。

2026-09-15のv1.7採用でIEFのターゲットが25%→0%になったが、保有分は評価額の4.6%で
±5ポイントの乖離バンド以内のため発動条件1（従来の両方向乖離ルール）が発動せず
売れないまま放置されていた。目標0%は±5pt以内でも全量売却するルールを追加した。
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import portfolio
from market import TickerSnapshot


def _state(**overrides):
    base = {
        "start_date": "2026-08-05", "mode": "normal", "cash_usd": 1000.0,
        "holdings": {}, "bench_units": 10.0, "last_processed_voo_date": None,
        "below_200dma_streak": 0, "above_200dma_streak": 0, "pending_orders": [],
    }
    base.update(overrides)
    return base


def _snap(ticker, close, date="2026-10-01"):
    return TickerSnapshot(ticker=ticker, close=close, date=date)


class TestComputeForcedZeroTargetSells(unittest.TestCase):
    def test_a_target_zero_weight_4_6pct_produces_full_sell(self):
        # NAV $64,270前後・IEF 33株@$89.55=$2,955.15 ≒ 4.6%。ターゲット0%なので
        # ±5pt以内でも全量売却の注文が作られる。
        state = _state(cash_usd=1351.14, holdings={"QQQ": 21.0, "IEF": 33.0})
        market = {"QQQ": _snap("QQQ", 580.0), "IEF": _snap("IEF", 89.55)}
        charter_targets = {"QQQ": {"normal": 0.25, "defense": 0.05}, "IEF": {"normal": 0.0, "defense": 0.25}}

        orders = portfolio.compute_forced_zero_target_sells(state, market, charter_targets)

        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["action"], "SELL")
        self.assertEqual(orders[0]["ticker"], "IEF")
        self.assertEqual(orders[0]["rule"], "rebalance")
        self.assertAlmostEqual(orders[0]["amount_usd"], 33.0 * 89.55)

    def test_a_execute_trades_allows_full_sell_despite_band(self):
        # 発動条件1の±5pt乖離バンド（4.6pt<5pt）に関わらず、execute_tradesでガードレールに
        # 拒否されず保有全株（33株）が売却されることを確認する。
        state = _state(cash_usd=1351.14, holdings={"QQQ": 21.0, "IEF": 33.0})
        market = {"QQQ": _snap("QQQ", 580.0), "IEF": _snap("IEF", 89.55)}
        charter_targets = {"QQQ": {"normal": 0.25, "defense": 0.05}, "IEF": {"normal": 0.0, "defense": 0.25}}
        orders = portfolio.compute_forced_zero_target_sells(state, market, charter_targets)

        with patch("portfolio.broker.get_cash", side_effect=[1351.14, 4306.69]), \
             patch("portfolio.broker.place_market_order", return_value={
                 "order_id": "1", "status": "FILLED_ALL", "filled_qty": 33, "avg_price": 89.55,
             }) as mock_place:
            new_state, accepted, rejected, queued = portfolio.execute_trades(
                orders, state, market, charter_targets, "2026-10-01",
            )

        self.assertEqual(rejected, [])
        self.assertEqual(queued, [])
        self.assertEqual(len(accepted), 1)
        mock_place.assert_called_once_with("IEF", 33, "SELL")
        self.assertNotIn("IEF", new_state["holdings"])

    def test_b_nonzero_target_within_band_produces_no_forced_sell(self):
        # ターゲット5%・乖離4.6pt（9.6%保有）はバンド内であり、かつターゲットが0%でもないため
        # 強制売却の対象外（バンドの挙動自体は変えていないことの確認）。
        state = _state(cash_usd=1000.0, holdings={"EWJ": 66.0})
        market = {"EWJ": _snap("EWJ", 98.0)}
        charter_targets = {"EWJ": {"normal": 0.05, "defense": 0.05}}

        orders = portfolio.compute_forced_zero_target_sells(state, market, charter_targets)

        self.assertEqual(orders, [])

    def test_c_target_zero_but_zero_shares_produces_no_order(self):
        state = _state(cash_usd=1000.0, holdings={"IEF": 0.0})
        market = {"IEF": _snap("IEF", 89.55)}
        charter_targets = {"IEF": {"normal": 0.0, "defense": 0.25}}

        orders = portfolio.compute_forced_zero_target_sells(state, market, charter_targets)

        self.assertEqual(orders, [])

    def test_c_target_zero_holding_absent_produces_no_order(self):
        state = _state(cash_usd=1000.0, holdings={})
        market = {"IEF": _snap("IEF", 89.55)}
        charter_targets = {"IEF": {"normal": 0.0, "defense": 0.25}}

        orders = portfolio.compute_forced_zero_target_sells(state, market, charter_targets)

        self.assertEqual(orders, [])

    def test_no_charter_targets_produces_no_order(self):
        state = _state(cash_usd=1000.0, holdings={"IEF": 33.0})
        market = {"IEF": _snap("IEF", 89.55)}

        orders = portfolio.compute_forced_zero_target_sells(state, market, None)

        self.assertEqual(orders, [])


if __name__ == "__main__":
    unittest.main()
