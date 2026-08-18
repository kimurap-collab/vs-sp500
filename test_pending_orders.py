"""pending_orders（未決注文の追跡・決済）の単体テスト。

保有照合の廃止（2026-08-18仕様変更）に伴い、各枠は自分の出したpending_ordersだけを
追跡・決済する。moomoo実接続は行わず、broker.get_order_status / broker.get_cash /
broker.place_market_order をモックして判定ロジックだけを検証する。

検証2 a〜e（SPEC変更）に対応する:
    a. 未決注文が全量約定 → 保有・現金・trades.csvに反映されpending_ordersから消える
    b. 一部約定 → 約定分だけ反映され、残数がpending_ordersに残る
    c. キャンセル → 台帳は無変更、pending_ordersから消え、警告が出る
    d. まだSUBMITTED → 台帳・pending_ordersとも無変更
    e. RSI枠のロットに対する決済（entryで建ったロットが後日約定した場合、ロットが正しく生成される）

実行: python3 -m unittest test_pending_orders.py -v
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import broker
import portfolio
import rsi_daily
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
# broker.classify_fill（純粋関数。分類ロジックの基礎テスト）
# ---------------------------------------------------------------------------

class TestClassifyFill(unittest.TestCase):
    def test_full(self):
        self.assertEqual(
            broker.classify_fill(10, {"filled_qty": 10, "status": "FILLED_ALL"}), "FULL",
        )

    def test_partial_open(self):
        self.assertEqual(
            broker.classify_fill(10, {"filled_qty": 4, "status": "SUBMITTED"}), "PARTIAL_OPEN",
        )

    def test_partial_terminal(self):
        self.assertEqual(
            broker.classify_fill(10, {"filled_qty": 4, "status": "FILL_CANCELLED"}), "PARTIAL_TERMINAL",
        )

    def test_none_open(self):
        self.assertEqual(
            broker.classify_fill(10, {"filled_qty": 0, "status": "SUBMITTED"}), "NONE_OPEN",
        )

    def test_none_terminal(self):
        self.assertEqual(
            broker.classify_fill(10, {"filled_qty": 0, "status": "CANCELLED_ALL"}), "NONE_TERMINAL",
        )


# ---------------------------------------------------------------------------
# 検証2-a: 全量約定
# ---------------------------------------------------------------------------

class TestMainSettlementFullFill(unittest.TestCase):
    def test_full_fill_applies_and_clears_pending(self):
        state = _main_state(cash_usd=10000.0, pending_orders=[{
            "order_id": "1", "ticker": "EWJ", "side": "BUY", "qty": 10,
            "submitted_date": "2026-08-17", "applied_qty": 0, "applied_value_usd": 0.0, "rule": "rebalance",
        }])
        with patch("portfolio.broker.get_order_status", return_value={
            "order_id": "1", "status": "FILLED_ALL", "filled_qty": 10, "avg_price": 50.0,
            "updated_date": "2026-08-18",
        }):
            new_state, applied, warnings = portfolio.settle_pending_orders(state, "2026-08-18")
        self.assertEqual(new_state["holdings"]["EWJ"], 10)
        self.assertAlmostEqual(new_state["cash_usd"], 10000.0 - 500.0)
        self.assertEqual(new_state["pending_orders"], [])
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0]["shares"], 10)
        self.assertEqual(applied[0]["action"], "BUY")


class TestMainSettlementIncrementalAfterPartial(unittest.TestCase):
    """既に一部が適用済みのpendingが、その後さらに約定した場合の増分価格計算を検証する。"""

    def test_second_partial_fill_uses_incremental_price(self):
        pending = [{
            "order_id": "6", "ticker": "IEF", "side": "BUY", "qty": 10,
            "submitted_date": "2026-08-17", "applied_qty": 4, "applied_value_usd": 400.0,  # 既に4株@100適用済み
            "rule": "rebalance",
        }]
        state = _main_state(cash_usd=10000.0, holdings={"IEF": 4.0}, pending_orders=pending)
        # 累計dealt_qty=10・dealt_avg_price=105 → 総額1050。既に400ドル分適用済みなので
        # 新規分は6株・650ドル（平均108.3333...）
        with patch("portfolio.broker.get_order_status", return_value={
            "order_id": "6", "status": "FILLED_ALL", "filled_qty": 10, "avg_price": 105.0,
            "updated_date": "2026-08-18",
        }):
            new_state, applied, warnings = portfolio.settle_pending_orders(state, "2026-08-18")
        self.assertEqual(new_state["holdings"]["IEF"], 10)
        self.assertEqual(applied[0]["shares"], 6)
        self.assertAlmostEqual(applied[0]["price"], 650 / 6, places=4)
        self.assertAlmostEqual(new_state["cash_usd"], 10000.0 - 650.0)
        self.assertEqual(new_state["pending_orders"], [])


# ---------------------------------------------------------------------------
# 検証2-b: 一部約定
# ---------------------------------------------------------------------------

class TestMainSettlementPartialFill(unittest.TestCase):
    def test_partial_fill_keeps_remainder_pending(self):
        state = _main_state(cash_usd=10000.0, pending_orders=[{
            "order_id": "2", "ticker": "QQQ", "side": "BUY", "qty": 10,
            "submitted_date": "2026-08-17", "applied_qty": 0, "applied_value_usd": 0.0, "rule": "rebalance",
        }])
        with patch("portfolio.broker.get_order_status", return_value={
            "order_id": "2", "status": "SUBMITTED", "filled_qty": 4, "avg_price": 100.0,
            "updated_date": "2026-08-18",
        }):
            new_state, applied, warnings = portfolio.settle_pending_orders(state, "2026-08-18")
        self.assertEqual(new_state["holdings"]["QQQ"], 4)
        self.assertAlmostEqual(new_state["cash_usd"], 10000.0 - 400.0)
        self.assertEqual(len(new_state["pending_orders"]), 1)
        self.assertEqual(new_state["pending_orders"][0]["applied_qty"], 4)
        self.assertEqual(new_state["pending_orders"][0]["qty"], 10)
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0]["shares"], 4)


# ---------------------------------------------------------------------------
# 検証2-c: キャンセル（一度も約定していないケース）
# ---------------------------------------------------------------------------

class TestMainSettlementCancelled(unittest.TestCase):
    def test_cancelled_before_any_fill_leaves_ledger_untouched(self):
        state = _main_state(cash_usd=10000.0, pending_orders=[{
            "order_id": "3", "ticker": "GLD", "side": "BUY", "qty": 5,
            "submitted_date": "2026-08-17", "applied_qty": 0, "applied_value_usd": 0.0, "rule": "rebalance",
        }])
        with patch("portfolio.broker.get_order_status", return_value={
            "order_id": "3", "status": "CANCELLED_ALL", "filled_qty": 0, "avg_price": 0.0,
            "updated_date": "2026-08-18",
        }):
            new_state, applied, warnings = portfolio.settle_pending_orders(state, "2026-08-18")
        self.assertEqual(new_state["holdings"], {})
        self.assertEqual(new_state["cash_usd"], 10000.0)
        self.assertEqual(new_state["pending_orders"], [])
        self.assertEqual(applied, [])
        self.assertTrue(any("終端" in w for w in warnings), warnings)


# ---------------------------------------------------------------------------
# 検証2-d: まだSUBMITTED
# ---------------------------------------------------------------------------

class TestMainSettlementStillSubmitted(unittest.TestCase):
    def test_still_submitted_leaves_everything_unchanged(self):
        pending = [{
            "order_id": "4", "ticker": "XLV", "side": "BUY", "qty": 5,
            "submitted_date": "2026-08-17", "applied_qty": 0, "applied_value_usd": 0.0, "rule": "rebalance",
        }]
        state = _main_state(cash_usd=10000.0, pending_orders=[dict(p) for p in pending])
        with patch("portfolio.broker.get_order_status", return_value={
            "order_id": "4", "status": "SUBMITTED", "filled_qty": 0, "avg_price": 0.0, "updated_date": None,
        }):
            new_state, applied, warnings = portfolio.settle_pending_orders(state, "2026-08-18")
        self.assertEqual(new_state["holdings"], {})
        self.assertEqual(new_state["cash_usd"], 10000.0)
        self.assertEqual(new_state["pending_orders"], pending)
        self.assertEqual(applied, [])


class TestMainSettlementQueryFailure(unittest.TestCase):
    def test_query_failure_keeps_entry_untouched(self):
        pending = [{
            "order_id": "5", "ticker": "TLT", "side": "SELL", "qty": 3,
            "submitted_date": "2026-08-17", "applied_qty": 0, "applied_value_usd": 0.0, "rule": "rebalance",
        }]
        state = _main_state(pending_orders=[dict(p) for p in pending])
        with patch("portfolio.broker.get_order_status", return_value=None):
            new_state, applied, warnings = portfolio.settle_pending_orders(state, "2026-08-18")
        self.assertEqual(new_state["pending_orders"], pending)
        self.assertEqual(applied, [])


# ---------------------------------------------------------------------------
# 発注時（execute_trades）の挙動: その場で約定しなかった場合は台帳を変えずキューに積む
# ---------------------------------------------------------------------------

class TestExecuteTradesQueuesUnfilledOrder(unittest.TestCase):
    def test_unfilled_buy_is_queued_not_rejected_and_not_applied(self):
        state = _main_state(cash_usd=100000.0, holdings={})
        market = {"VOO": _snap("VOO", 100.0)}
        trade = {"action": "BUY", "ticker": "VOO", "amount_usd": 1000.0, "rule": "rebalance"}
        with patch("portfolio.broker.get_cash", return_value=100000.0), \
             patch("portfolio.broker.place_market_order", return_value={
                 "order_id": "77", "status": "SUBMITTED", "filled_qty": 0, "avg_price": 0.0,
             }):
            new_state, accepted, rejected, queued = portfolio.execute_trades(
                [trade], state, market, None, "2026-08-18",
            )
        self.assertEqual(new_state["holdings"], {})
        self.assertEqual(new_state["cash_usd"], 100000.0)
        self.assertEqual(accepted, [])
        self.assertEqual(rejected, [])
        self.assertEqual(len(queued), 1)
        self.assertEqual(len(new_state["pending_orders"]), 1)
        self.assertEqual(new_state["pending_orders"][0]["order_id"], "77")
        self.assertEqual(new_state["pending_orders"][0]["qty"], 10)


class TestExecuteTradesPartialFillAppliedAndQueued(unittest.TestCase):
    def test_partial_fill_applies_partial_and_queues_remainder(self):
        state = _main_state(cash_usd=100000.0, holdings={})
        market = {"VOO": _snap("VOO", 100.0)}
        trade = {"action": "BUY", "ticker": "VOO", "amount_usd": 1000.0, "rule": "rebalance"}
        with patch("portfolio.broker.get_cash", side_effect=[100000.0, 99600.0]), \
             patch("portfolio.broker.place_market_order", return_value={
                 "order_id": "78", "status": "SUBMITTED", "filled_qty": 4, "avg_price": 100.0,
             }):
            new_state, accepted, rejected, queued = portfolio.execute_trades(
                [trade], state, market, None, "2026-08-18",
            )
        self.assertEqual(new_state["holdings"]["VOO"], 4)
        self.assertAlmostEqual(new_state["cash_usd"], 99600.0)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["shares"], 4)
        self.assertEqual(len(new_state["pending_orders"]), 1)
        self.assertEqual(new_state["pending_orders"][0]["applied_qty"], 4)
        self.assertEqual(new_state["pending_orders"][0]["qty"], 10)


# ---------------------------------------------------------------------------
# 検証2-e: RSI枠のロットに対する決済（entryロットは決済時に初めて生成される）
# ---------------------------------------------------------------------------

class TestRsiSettlementEntryLotCreation(unittest.TestCase):
    def test_entry_fill_creates_lot(self):
        rsi_state = _rsi_state(pending_orders=[{
            "order_id": "9", "ticker": "AAPL", "side": "BUY", "qty": 100,
            "submitted_date": "2026-08-17", "applied_qty": 0, "applied_value_usd": 0.0,
            "rule": "entry", "lot_id": "AAPL-2026-08-17-1",
        }])
        with patch("rsi_daily.broker.get_order_status", return_value={
            "order_id": "9", "status": "FILLED_ALL", "filled_qty": 100, "avg_price": 150.0,
            "updated_date": "2026-08-18",
        }):
            new_state, applied, warnings = rsi_daily.settle_pending_orders(rsi_state, "2026-08-18")
        self.assertEqual(len(new_state["lots"]), 1)
        lot = new_state["lots"][0]
        self.assertEqual(lot["lot_id"], "AAPL-2026-08-17-1")
        self.assertEqual(lot["shares"], 100)
        self.assertEqual(lot["initial_entry_price"], 150.0)
        self.assertEqual(lot["initial_entry_date"], "2026-08-18")  # 実際の約定日を採用
        self.assertAlmostEqual(new_state["cash_usd"], 500000.0 - 15000.0)
        self.assertEqual(new_state["pending_orders"], [])
        self.assertEqual(len(applied), 1)


class TestRsiSettlementPyramidOnExistingLot(unittest.TestCase):
    def test_pyramid_fill_updates_existing_lot(self):
        existing_lot = {
            "lot_id": "AAPL-2026-08-10-1", "ticker": "AAPL",
            "initial_entry_date": "2026-08-10", "initial_entry_price": 100.0,
            "pyramid_done": [False, False, False], "shares": 300,
            "total_invested_usd": 30000.0, "avg_cost": 100.0,
            "profit1_taken": False, "profit2_taken": False, "base_shares": None,
            "exception_active": False, "exception_deadline_date": None,
            "closed": False, "closed_reason": None, "closed_date": None,
        }
        rsi_state = _rsi_state(lots=[existing_lot], pending_orders=[{
            "order_id": "10", "ticker": "AAPL", "side": "BUY", "qty": 146,
            "submitted_date": "2026-08-17", "applied_qty": 0, "applied_value_usd": 0.0,
            "rule": "pyramid1", "lot_id": "AAPL-2026-08-10-1", "stage_index": 0,
        }])
        with patch("rsi_daily.broker.get_order_status", return_value={
            "order_id": "10", "status": "FILLED_ALL", "filled_qty": 146, "avg_price": 102.5,
            "updated_date": "2026-08-18",
        }):
            new_state, applied, warnings = rsi_daily.settle_pending_orders(rsi_state, "2026-08-18")
        self.assertEqual(len(new_state["lots"]), 1)
        lot = new_state["lots"][0]
        self.assertTrue(lot["pyramid_done"][0])
        self.assertEqual(lot["shares"], 300 + 146)
        self.assertEqual(new_state["pending_orders"], [])


if __name__ == "__main__":
    unittest.main()
