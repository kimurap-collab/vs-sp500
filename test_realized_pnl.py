"""実現損益(realized_pnl/realized_pnl_pct)の単体テスト（2026-10-06追加）。

大将「損切りや利益確定した際の履歴にいくら損や得をしたか書いておいてほしい」
（択一１＝全3枠・２＝台帳とダッシュボード両方・３＝過去の売りもバックフィル）。

検証範囲:
  - portfolio.compute_realized_pnl / rsi_strategy.compute_realized_pnl の単体計算
  - 本体execute_trades: リバランスSELL・損切りSELLでrealized_pnlが記帳されること
  - 米国RSI枠run(): 損切りでrealized_pnlが記帳されること
  - 日本株RSI枠run_jp(): 利確でrealized_pnlが記帳されること
  - trades.csvの読み出し（新列込み）・report.pyのdata.json組み立てが新列を保持すること
  - backfill_realized_pnl.py の再生ロジック（過去SELL行へのバックフィル）

実データ・moomoo/yfinance接続は一切使わない。
実行: python3 -m unittest test_realized_pnl.py -v
"""
from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import backfill_realized_pnl as backfill
import config
import jp_rsi_daily
import jp_rsi_ledger
import portfolio
import report
import rsi_daily
import rsi_ledger
import rsi_strategy
from jp_market import JpSnapshot
from market import TickerSnapshot

FILLED_STOP = {"order_id": "1", "status": "FILLED_ALL", "filled_qty": 100, "avg_price": 90.0}
FILLED_PROFIT1 = {"order_id": "2", "status": "FILLED_ALL", "filled_qty": 50, "avg_price": 120.12}


# ---------------------------------------------------------------------------
# 純粋関数: portfolio.compute_realized_pnl / rsi_strategy.compute_realized_pnl
# ---------------------------------------------------------------------------

class TestPortfolioComputeRealizedPnl(unittest.TestCase):
    def test_profit_minus_fee(self):
        pnl, pct = portfolio.compute_realized_pnl(avg_cost=100.0, sell_price=110.0, shares=10, fee=1.0)
        self.assertEqual(pnl, 99.0)  # (110-100)*10 - 1
        self.assertEqual(pct, 10.0)

    def test_loss(self):
        pnl, pct = portfolio.compute_realized_pnl(avg_cost=100.0, sell_price=90.0, shares=10, fee=1.0)
        self.assertEqual(pnl, -101.0)  # (90-100)*10 - 1
        self.assertAlmostEqual(pct, -10.0)

    def test_no_fee_defaults_to_zero(self):
        pnl, pct = portfolio.compute_realized_pnl(avg_cost=50.0, sell_price=55.0, shares=4)
        self.assertEqual(pnl, 20.0)
        self.assertEqual(pct, 10.0)

    def test_unknown_avg_cost_returns_empty_strings(self):
        pnl, pct = portfolio.compute_realized_pnl(avg_cost=None, sell_price=55.0, shares=4)
        self.assertEqual(pnl, "")
        self.assertEqual(pct, "")

    def test_zero_avg_cost_returns_empty_strings(self):
        pnl, pct = portfolio.compute_realized_pnl(avg_cost=0.0, sell_price=55.0, shares=4)
        self.assertEqual(pnl, "")
        self.assertEqual(pct, "")


class TestRsiStrategyComputeRealizedPnl(unittest.TestCase):
    def test_profit(self):
        pnl, pct = rsi_strategy.compute_realized_pnl(100.0, 120.0, 50)
        self.assertEqual(pnl, 1000.0)
        self.assertEqual(pct, 20.0)

    def test_loss(self):
        pnl, pct = rsi_strategy.compute_realized_pnl(100.0, 92.0, 100)
        self.assertEqual(pnl, -800.0)
        self.assertEqual(pct, -8.0)


# ---------------------------------------------------------------------------
# 本体: execute_trades経由のSELL（リバランス・損切り）
# ---------------------------------------------------------------------------

def _main_state(**overrides):
    base = {
        "start_date": "2026-08-05", "mode": "normal", "cash_usd": 10000.0,
        "holdings": {"GLD": 10.0}, "bench_units": 10.0, "last_processed_voo_date": None,
        "below_200dma_streak": 0, "above_200dma_streak": 0, "pending_orders": [],
    }
    base.update(overrides)
    return base


def _snap(ticker, close, date="2026-09-15"):
    return TickerSnapshot(ticker=ticker, close=close, date=date)


class TestMainFrameSellRealizedPnl(unittest.TestCase):
    def _run_sell(self, rule, fill_price, fee):
        trades_csv = (
            "date,action,ticker,shares,price,currency,amount_usd,fee_usd,rule,note,"
            "realized_pnl,realized_pnl_pct\r\n"
            "2026-08-05,BUY,GLD,10,100.0,USD,1000.0,1.0,initial_build,,,\r\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "trades.csv"
            fake_path.write_text(trades_csv, encoding="utf-8")
            state = _main_state()
            market = {"GLD": _snap("GLD", fill_price)}
            trade = {"action": "SELL", "ticker": "GLD", "amount_usd": 10 * fill_price, "rule": rule}
            with patch.object(config, "TRADES_CSV_PATH", fake_path), \
                 patch("portfolio.broker.get_cash", side_effect=[0.0, -fee + 10 * fill_price]), \
                 patch("portfolio.broker.place_market_order", return_value={
                     "order_id": "9", "status": "FILLED_ALL", "filled_qty": 10, "avg_price": fill_price,
                 }):
                _, accepted, rejected, _ = portfolio.execute_trades(
                    [trade], state, market, None, "2026-09-15",
                )
        self.assertEqual(rejected, [])
        self.assertEqual(len(accepted), 1)
        return accepted[0]

    def test_rebalance_sell_profit(self):
        row = self._run_sell("rebalance", 110.0, fee=1.0)
        self.assertEqual(row["realized_pnl"], 99.0)  # (110-100)*10 - 1
        self.assertAlmostEqual(row["realized_pnl_pct"], 10.0)

    def test_stop_loss_sell_loss(self):
        row = self._run_sell("stop_loss", 80.0, fee=1.0)
        self.assertEqual(row["realized_pnl"], -201.0)  # (80-100)*10 - 1
        self.assertAlmostEqual(row["realized_pnl_pct"], -20.0)


# ---------------------------------------------------------------------------
# 米国RSI枠: run()経由の損切りSELL
# ---------------------------------------------------------------------------

class TestUsRsiStopLossRealizedPnl(unittest.TestCase):
    def test_stop_loss_records_realized_pnl(self):
        lot = rsi_strategy.new_lot("TST", "TST-2026-08-01-1", "2026-08-01", 100, 100.0)
        state = {
            "start_date": "2026-08-01", "cash_usd": 100_000.0, "lots": [lot],
            "bench_units_rsi": 100.0, "last_processed_date": "2026-09-28", "pending_orders": [],
        }
        voo_snap = TickerSnapshot(ticker="VOO", close=700.0, date="2026-09-29")
        with tempfile.TemporaryDirectory() as tmp:
            fake_frozen = Path(tmp) / "frozen_candidates.json"  # 存在しない
            with patch.object(config, "RSI_FROZEN_CANDIDATES_PATH", fake_frozen), \
                 patch("rsi_daily.screen_rsi_candidates", return_value=[]), \
                 patch("rsi_daily.broker.get_splits", return_value={"TST": []}), \
                 patch("rsi_daily.broker.get_dividends", return_value={"TST": []}), \
                 patch("rsi_daily.fetch_market_data", return_value={"TST": {"close": 90.0, "date": "2026-09-29"}}), \
                 patch("rsi_daily.broker.get_cash", side_effect=[0.0, 100 * 90.0]), \
                 patch("rsi_daily.broker.place_market_order", return_value=FILLED_STOP), \
                 patch("rsi_daily.rsi_ledger.append_trade_row") as mock_append, \
                 patch("rsi_daily.rsi_ledger.append_history_row"), \
                 patch("rsi_daily.rsi_ledger.save_portfolio"):
                new_state, accepted, log_lines, nav, bench, held = rsi_daily.run(
                    state, voo_snap, can_trade=True, already_processed_today=False,
                    dry_run=False, trade_date="2026-09-29", market_us=None,
                )
        self.assertEqual(len(accepted), 1)
        trade_row = accepted[0]
        self.assertEqual(trade_row["rule"], "stop_loss")
        self.assertEqual(trade_row["realized_pnl"], -1000.0)  # (90-100)*100
        self.assertAlmostEqual(trade_row["realized_pnl_pct"], -10.0)
        mock_append.assert_called_once()
        self.assertEqual(mock_append.call_args[0][0]["realized_pnl"], -1000.0)


# ---------------------------------------------------------------------------
# 日本株RSI枠: run_jp()経由の利確SELL
# ---------------------------------------------------------------------------

class TestJpRsiProfitTakeRealizedPnl(unittest.TestCase):
    def test_profit1_records_realized_pnl(self):
        lot = rsi_strategy.new_lot("1234", "1234-2026-07-01-1", "2026-07-01", 100, 100.0, 100)
        lot["pyramid_done"] = [True, True, True]  # 買い増しが同時発火してaccepted件数がずれるのを防ぐ
        state = {"start_date": "2026-07-01", "cash_jpy": 1_000_000.0, "lots": [lot], "last_processed_date": "2026-09-28"}
        snaps = {"1234": JpSnapshot("1234", 120.12, "2026-09-29")}  # avg_cost*1.20*1.001超
        with patch("jp_rsi_daily.jp_market.get_splits", return_value=[]), \
             patch("jp_rsi_daily.jp_market.get_snapshots", return_value=snaps), \
             patch("jp_rsi_daily.jp_market.get_dividends", return_value=[]), \
             patch("jp_rsi_daily.get_jp_candidates", return_value=[]), \
             patch("jp_rsi_daily.jp_lotsize.get_lot_sizes", return_value={}), \
             patch("jp_rsi_daily.jp_lotsize.get_company_tickers", return_value=set()), \
             patch("jp_rsi_daily.jp_rsi_ledger.append_trade_row") as mock_append, \
             patch("jp_rsi_daily.jp_rsi_ledger.append_history_row"), \
             patch("jp_rsi_daily.jp_rsi_ledger.save_portfolio"):
            new_state, accepted, log_lines, nav, held = jp_rsi_daily.run_jp(state, "2026-09-29", dry_run=False)
        self.assertEqual(len(accepted), 1)
        trade_row = accepted[0]
        self.assertEqual(trade_row["rule"], "profit1")
        self.assertEqual(trade_row["realized_pnl"], 1006.0)  # (120.12-100)*50
        self.assertAlmostEqual(trade_row["realized_pnl_pct"], 20.12)
        mock_append.assert_called_once()
        self.assertEqual(mock_append.call_args[0][0]["realized_pnl"], 1006.0)


# ---------------------------------------------------------------------------
# CSVリーダー・data.json組み立てが新列を保持すること
# ---------------------------------------------------------------------------

class TestCsvReadersKeepNewColumns(unittest.TestCase):
    def test_portfolio_read_trade_rows_includes_realized_pnl(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "trades.csv"
            fake_path.write_text(
                "date,action,ticker,shares,price,currency,amount_usd,fee_usd,rule,note,"
                "realized_pnl,realized_pnl_pct\r\n"
                "2026-09-15,SELL,GLD,10,110.0,USD,1100.0,1.0,rebalance,,99.0,10.0\r\n",
                encoding="utf-8",
            )
            with patch.object(config, "TRADES_CSV_PATH", fake_path):
                rows = portfolio.read_trade_rows()
        self.assertEqual(rows[0]["realized_pnl"], "99.0")
        self.assertEqual(rows[0]["realized_pnl_pct"], "10.0")

    def test_rsi_ledger_read_trade_rows_includes_realized_pnl(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "trades.csv"
            fake_path.write_text(
                "date,action,ticker,shares,price,amount_usd,rule,lot_id,note,name,"
                "realized_pnl,realized_pnl_pct\r\n"
                "2026-09-28,SELL,TST,100,90.0,9000.0,stop_loss,TST-1,,Test Co,-1000.0,-10.0\r\n",
                encoding="utf-8",
            )
            with patch.object(config, "RSI_TRADES_CSV_PATH", fake_path):
                rows = rsi_ledger.read_trade_rows()
        self.assertEqual(rows[0]["realized_pnl"], "-1000.0")

    def test_jp_rsi_ledger_read_trade_rows_includes_realized_pnl(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "trades.csv"
            fake_path.write_text(
                "date,action,ticker,shares,price,amount_jpy,rule,lot_id,note,name,"
                "realized_pnl,realized_pnl_pct\r\n"
                "2026-09-28,SELL,1234,50,120.12,6006.0,profit1,1234-1,,Test KK,1006.0,20.12\r\n",
                encoding="utf-8",
            )
            with patch.object(config, "RSI_JP_TRADES_CSV_PATH", fake_path):
                rows = jp_rsi_ledger.read_trade_rows()
        self.assertEqual(rows[0]["realized_pnl"], "1006.0")

    def test_build_data_json_trades_carry_realized_pnl(self):
        """report.build_data_jsonが組み立てるtrades配列がrealized_pnl列を保持すること。"""
        with tempfile.TemporaryDirectory() as tmp:
            trades_path = Path(tmp) / "trades.csv"
            history_path = Path(tmp) / "history.csv"
            trades_path.write_text(
                "date,action,ticker,shares,price,currency,amount_usd,fee_usd,rule,note,"
                "realized_pnl,realized_pnl_pct\r\n"
                "2026-09-15,SELL,GLD,10,110.0,USD,1100.0,1.0,rebalance,,99.0,10.0\r\n",
                encoding="utf-8",
            )
            history_path.write_text("date,nav_usd,bench_usd,diff_usd,diff_pct,cash_ratio\r\n", encoding="utf-8")
            state = {"holdings": {}, "cash_usd": 0.0, "start_date": "2026-08-05", "mode": "normal"}
            with patch.object(config, "TRADES_CSV_PATH", trades_path), \
                 patch.object(config, "HISTORY_CSV_PATH", history_path):
                data = report.build_data_json(
                    state, {}, nav_usd=0.0, bench_usd=0.0, accepted_trades=[],
                    now_jst=__import__("datetime").datetime(2026, 10, 6),
                )
        self.assertEqual(data["trades"][0]["realized_pnl"], "99.0")
        self.assertEqual(data["trades"][0]["realized_pnl_pct"], "10.0")


# ---------------------------------------------------------------------------
# backfill_realized_pnl.py の再生ロジック
# ---------------------------------------------------------------------------

class TestReplayMainFrameRows(unittest.TestCase):
    def test_single_buy_then_sell(self):
        rows = [
            {"action": "BUY", "ticker": "GLD", "shares": "10", "price": "100.0",
             "amount_usd": "1000.0", "fee_usd": "1.0"},
            {"action": "SELL", "ticker": "GLD", "shares": "10", "price": "110.0",
             "amount_usd": "1100.0", "fee_usd": "1.0"},
        ]
        new_rows, avg_cost, shares = backfill.replay_main_frame_rows(rows)
        self.assertEqual(new_rows[0]["realized_pnl"], "")
        self.assertEqual(new_rows[1]["realized_pnl"], 99.0)  # (110-100)*10 - 1
        self.assertAlmostEqual(new_rows[1]["realized_pnl_pct"], 10.0)
        self.assertEqual(shares["GLD"], 0.0)

    def test_weighted_average_across_two_buys(self):
        rows = [
            {"action": "BUY", "ticker": "X", "shares": "10", "price": "100.0", "amount_usd": "1000.0", "fee_usd": "0"},
            {"action": "BUY", "ticker": "X", "shares": "10", "price": "120.0", "amount_usd": "1200.0", "fee_usd": "0"},
            {"action": "SELL", "ticker": "X", "shares": "20", "price": "121.0", "amount_usd": "2420.0", "fee_usd": "0"},
        ]
        new_rows, avg_cost, shares = backfill.replay_main_frame_rows(rows)
        # 平均取得単価=(10*100+10*120)/20=110。 (121-110)*20=220
        self.assertEqual(new_rows[2]["realized_pnl"], 220.0)
        self.assertEqual(shares["X"], 0.0)

    def test_sell_without_prior_buy_leaves_empty(self):
        rows = [{"action": "SELL", "ticker": "UNKNOWN", "shares": "5", "price": "10.0",
                  "amount_usd": "50.0", "fee_usd": "0"}]
        new_rows, _, _ = backfill.replay_main_frame_rows(rows)
        self.assertEqual(new_rows[0]["realized_pnl"], "")
        self.assertEqual(new_rows[0]["realized_pnl_pct"], "")


class TestReplayLotFrameRows(unittest.TestCase):
    def test_entry_then_pyramid_then_sell(self):
        rows = [
            {"action": "BUY", "lot_id": "A-1", "shares": "10", "price": "100.0"},
            {"action": "BUY", "lot_id": "A-1", "shares": "10", "price": "120.0"},
            {"action": "SELL", "lot_id": "A-1", "shares": "20", "price": "150.0"},
        ]
        new_rows, avg_cost, shares = backfill.replay_lot_frame_rows(rows)
        # avg_cost=(10*100+10*120)/20=110。 (150-110)*20=800
        self.assertEqual(new_rows[2]["realized_pnl"], 800.0)
        self.assertAlmostEqual(new_rows[2]["realized_pnl_pct"], (150 / 110 - 1) * 100, places=4)
        self.assertEqual(shares["A-1"], 0.0)

    def test_independent_lots_not_mixed(self):
        rows = [
            {"action": "BUY", "lot_id": "A-1", "shares": "10", "price": "100.0"},
            {"action": "BUY", "lot_id": "B-1", "shares": "10", "price": "200.0"},
            {"action": "SELL", "lot_id": "A-1", "shares": "10", "price": "90.0"},
        ]
        new_rows, avg_cost, shares = backfill.replay_lot_frame_rows(rows)
        self.assertEqual(new_rows[2]["realized_pnl"], -100.0)  # (90-100)*10, lot B avg_costは無関係
        self.assertEqual(avg_cost["B-1"], 200.0)


if __name__ == "__main__":
    unittest.main()
