"""発注前の市場状態ゲート（2026-08-18 修正2）の単体テスト。

新規発注（本体execute_trades・RSI枠の損切り/利確/買い増し/新規エントリーが通る
_execute_order）の直前にmarket_us（moomoo get_global_state()のmarket_us値）を確認し、
AFTERNOON以外なら発注せず見送ることを検証する。判定に失敗した場合（None）は
フェイルオープンで発注する。

問い合わせ自体（broker.get_market_us_state / broker.get_global_state）はdaily_run.py側が
1回だけ行い、値をここでテストする各関数へパラメータとして渡す設計のため、execute_trades・
_execute_orderの内部ではmoomooへの追加問い合わせが一切発生しないことも検証する
（「発注ごとに毎回叩かない」の担保）。

実行: python3 -m unittest test_market_open_gate.py -v
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

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


def _snap(ticker, close, date="2026-08-18"):
    return TickerSnapshot(ticker=ticker, close=close, date=date)


FILLED = {"order_id": "1", "status": "FILLED_ALL", "filled_qty": 5, "avg_price": 100.0}


# ---------------------------------------------------------------------------
# 本体（portfolio.execute_trades）
# ---------------------------------------------------------------------------

class TestMainOrderGate(unittest.TestCase):
    def test_afternoon_places_order(self):
        state = _main_state(cash_usd=10000.0)
        market = {"VOO": _snap("VOO", 100.0)}
        trade = {"action": "BUY", "ticker": "VOO", "amount_usd": 500.0, "rule": "rebalance"}
        with patch("portfolio.broker.get_cash", return_value=10000.0), \
             patch("portfolio.broker.place_market_order", return_value=FILLED) as mock_place:
            new_state, accepted, rejected, queued = portfolio.execute_trades(
                [trade], state, market, None, "2026-08-18", "AFTERNOON",
            )
        mock_place.assert_called_once()
        self.assertEqual(len(accepted), 1)
        self.assertEqual(rejected, [])

    def test_not_afternoon_skips_order_and_logs(self):
        state = _main_state(cash_usd=10000.0)
        market = {"VOO": _snap("VOO", 100.0)}
        trade = {"action": "BUY", "ticker": "VOO", "amount_usd": 500.0, "rule": "rebalance"}
        with patch("portfolio.broker.get_cash", return_value=10000.0), \
             patch("portfolio.broker.place_market_order") as mock_place, \
             self.assertLogs("vs-sp500.portfolio", level="INFO") as log_ctx:
            new_state, accepted, rejected, queued = portfolio.execute_trades(
                [trade], state, market, None, "2026-08-18", "AFTER_HOURS_END",
            )
        mock_place.assert_not_called()
        self.assertEqual(accepted, [])
        self.assertEqual(len(rejected), 1)
        self.assertIn("市場が開いていない", rejected[0]["reason"])
        self.assertTrue(any("market_us=AFTER_HOURS_END" in m for m in log_ctx.output))

    def test_none_market_state_places_order_fail_open(self):
        state = _main_state(cash_usd=10000.0)
        market = {"VOO": _snap("VOO", 100.0)}
        trade = {"action": "BUY", "ticker": "VOO", "amount_usd": 500.0, "rule": "rebalance"}
        with patch("portfolio.broker.get_cash", return_value=10000.0), \
             patch("portfolio.broker.place_market_order", return_value=FILLED) as mock_place:
            new_state, accepted, rejected, queued = portfolio.execute_trades(
                [trade], state, market, None, "2026-08-18", None,
            )
        mock_place.assert_called_once()
        self.assertEqual(len(accepted), 1)
        self.assertEqual(rejected, [])

    def test_gate_check_does_not_query_moomoo_itself(self):
        """execute_tradesはmarket_usを引数で受け取るだけで、内部でmoomooへの市場状態問い合わせは
        行わない（複数件の取引を処理しても問い合わせが増えないことを担保する）。
        """
        state = _main_state(cash_usd=10000.0)
        market = {"VOO": _snap("VOO", 100.0), "QQQ": _snap("QQQ", 100.0)}
        trades = [
            {"action": "BUY", "ticker": "VOO", "amount_usd": 300.0, "rule": "rebalance"},
            {"action": "BUY", "ticker": "QQQ", "amount_usd": 300.0, "rule": "rebalance"},
        ]
        with patch("portfolio.broker.get_cash", return_value=10000.0), \
             patch("portfolio.broker.place_market_order", return_value=FILLED), \
             patch("portfolio.broker.get_market_us_state") as mock_query, \
             patch("portfolio.broker.get_global_state") as mock_query2:
            portfolio.execute_trades(trades, state, market, None, "2026-08-18", "AFTERNOON")
        mock_query.assert_not_called()
        mock_query2.assert_not_called()


# ---------------------------------------------------------------------------
# RSI枠（rsi_daily._execute_order。損切り・利確・買い増し・新規エントリーの共通経路）
# ---------------------------------------------------------------------------

class TestRsiOrderGate(unittest.TestCase):
    def test_afternoon_places_order(self):
        with patch("rsi_daily.broker.get_cash", return_value=100000.0), \
             patch("rsi_daily.broker.place_market_order", return_value=FILLED) as mock_place:
            fill, cash_delta = rsi_daily._execute_order("AAPL", 5, "BUY", "AFTERNOON")
        mock_place.assert_called_once()
        self.assertIsNotNone(fill)

    def test_not_afternoon_skips_order_and_logs(self):
        with patch("rsi_daily.broker.get_cash") as mock_cash, \
             patch("rsi_daily.broker.place_market_order") as mock_place, \
             self.assertLogs("vs-sp500.rsi_daily", level="INFO") as log_ctx:
            fill, cash_delta = rsi_daily._execute_order("AAPL", 5, "BUY", "AFTER_HOURS_END")
        mock_cash.assert_not_called()
        mock_place.assert_not_called()
        self.assertIsNone(fill)
        self.assertEqual(cash_delta, 0.0)
        self.assertTrue(any("market_us=AFTER_HOURS_END" in m for m in log_ctx.output))

    def test_none_market_state_places_order_fail_open(self):
        with patch("rsi_daily.broker.get_cash", return_value=100000.0), \
             patch("rsi_daily.broker.place_market_order", return_value=FILLED) as mock_place:
            fill, cash_delta = rsi_daily._execute_order("AAPL", 5, "BUY", None)
        mock_place.assert_called_once()
        self.assertIsNotNone(fill)

    def test_gate_check_does_not_query_moomoo_itself(self):
        with patch("rsi_daily.broker.get_cash", return_value=100000.0), \
             patch("rsi_daily.broker.place_market_order", return_value=FILLED), \
             patch("rsi_daily.broker.get_market_us_state") as mock_query, \
             patch("rsi_daily.broker.get_global_state") as mock_query2:
            rsi_daily._execute_order("AAPL", 5, "BUY", "AFTERNOON")
            rsi_daily._execute_order("MSFT", 5, "BUY", "AFTERNOON")
        mock_query.assert_not_called()
        mock_query2.assert_not_called()


if __name__ == "__main__":
    unittest.main()
