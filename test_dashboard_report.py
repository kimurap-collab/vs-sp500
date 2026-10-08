"""report.py: ダッシュボード「今夜の候補」・保有一覧の規模(大/中/小)表示の単体テスト（2026-10-08追加）。

build_rsi_block/build_rsi_jp_blockのdashboard引数（rsi_daily/jp_rsi_daily の
build_dashboard_candidates*()が返す辞書）が、保有一覧のsize_label/market_cap と
候補一覧(candidates)に正しく反映されることを検証する。実データ・moomoo接続は使わない。
history.csv/trades.csv/dividends.csvは実ファイルを読ませず、一時ディレクトリに差し替える。
実行: python3 -m unittest test_dashboard_report.py -v
"""
from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import report
from market import TickerSnapshot


class TestBuildRsiBlockDashboard(unittest.TestCase):
    def test_holdings_get_size_label_and_market_cap_from_dashboard(self):
        rsi_state = {
            "start_date": "2026-01-01",
            "cash_usd": 0.0,
            "lots": [{
                "lot_id": "L1", "ticker": "AAPL", "name": "Apple", "shares": 10.0,
                "avg_cost": 150.0, "closed": False,
            }],
        }
        market = {"AAPL": TickerSnapshot(ticker="AAPL", close=160.0, date="2026-10-08")}
        dashboard = {
            "market_caps": {"AAPL": config.RSI_SWAP_MARKET_CAP_LARGE_MIN_USD},
            "candidates": [{
                "ticker": "MSFT", "name": "Microsoft", "rsi14": 22.0,
                "market_cap": config.RSI_SWAP_MARKET_CAP_SMALL_MAX_USD - 1.0,
                "size_label": "小", "sector_tier": 1, "score": 2,
            }],
            "as_of": "2026-10-08T12:00:00+00:00",
        }
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(config, "RSI_HISTORY_CSV_PATH", Path(tmp) / "history.csv"), \
                 patch.object(config, "RSI_TRADES_CSV_PATH", Path(tmp) / "trades.csv"), \
                 patch.object(config, "RSI_DIVIDENDS_CSV_PATH", Path(tmp) / "dividends.csv"):
                block = report.build_rsi_block(rsi_state, market, 1600.0, 1500.0, [], dashboard)

        holding = block["holdings"][0]
        self.assertEqual(holding["size_label"], "大")
        self.assertAlmostEqual(holding["market_cap_100m_usd"], config.RSI_SWAP_MARKET_CAP_LARGE_MIN_USD / 1e8, places=1)

        self.assertEqual(block["candidates_as_of"], "2026-10-08T12:00:00+00:00")
        cand = block["candidates"][0]
        self.assertEqual(cand["ticker"], "MSFT")
        self.assertEqual(cand["size_label"], "小")
        self.assertEqual(cand["sector_label"], "強")
        self.assertEqual(cand["score"], 2)

        # 含み損益（2026-10-09追加・Change2）: shares=10, avg_cost=150.0, price=160.0
        self.assertAlmostEqual(holding["unrealized_pnl_usd"], 100.0, places=2)
        self.assertAlmostEqual(holding["unrealized_pnl_pct"], (160.0 / 150.0 - 1) * 100, places=2)

    def test_none_dashboard_leaves_holdings_and_candidates_empty_defaults(self):
        rsi_state = {"start_date": "2026-01-01", "cash_usd": 0.0, "lots": []}
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(config, "RSI_HISTORY_CSV_PATH", Path(tmp) / "history.csv"), \
                 patch.object(config, "RSI_TRADES_CSV_PATH", Path(tmp) / "trades.csv"), \
                 patch.object(config, "RSI_DIVIDENDS_CSV_PATH", Path(tmp) / "dividends.csv"):
                block = report.build_rsi_block(rsi_state, {}, 0.0, 0.0, [])
        self.assertEqual(block["candidates"], [])
        self.assertIsNone(block["candidates_as_of"])


class TestBuildRsiJpBlockDashboard(unittest.TestCase):
    def test_holdings_get_size_label_and_market_cap_from_dashboard(self):
        jp_state = {
            "start_date": "2026-01-01",
            "cash_jpy": 0.0,
            "lots": [{
                "lot_id": "L1", "ticker": "7203", "name": "Toyota", "shares": 100.0,
                "avg_cost": 2000.0, "closed": False,
            }],
        }
        market = {"7203": TickerSnapshot(ticker="7203", close=2100.0, date="2026-10-08")}
        dashboard = {
            "market_caps": {"7203": config.RSI_JP_SWAP_MARKET_CAP_LARGE_MIN_JPY},
            "candidates": [{
                "ticker": "9999", "name": "Example Co", "rsi14": 18.0,
                "market_cap": config.RSI_JP_SWAP_MARKET_CAP_SMALL_MAX_JPY - 1.0,
                "size_label": "小", "sector_tier": -1, "score": 0,
            }],
            "as_of": "2026-10-08T12:00:00+00:00",
        }
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(config, "RSI_JP_HISTORY_CSV_PATH", Path(tmp) / "history.csv"), \
                 patch.object(config, "RSI_JP_TRADES_CSV_PATH", Path(tmp) / "trades.csv"), \
                 patch.object(config, "RSI_JP_DIVIDENDS_CSV_PATH", Path(tmp) / "dividends.csv"):
                block = report.build_rsi_jp_block(jp_state, market, 210_000.0, [], dashboard)

        holding = block["holdings"][0]
        self.assertEqual(holding["size_label"], "大")
        self.assertAlmostEqual(
            holding["market_cap_100m_jpy"], config.RSI_JP_SWAP_MARKET_CAP_LARGE_MIN_JPY / 1e8, places=1,
        )

        cand = block["candidates"][0]
        self.assertEqual(cand["sector_label"], "弱")
        self.assertEqual(cand["score"], 0)

        # 含み損益（2026-10-09追加・Change2）: shares=100, avg_cost=2000.0, price=2100.0
        self.assertAlmostEqual(holding["unrealized_pnl_jpy"], 10000.0, places=0)
        self.assertAlmostEqual(holding["unrealized_pnl_pct"], 5.0, places=2)

    def test_none_dashboard_leaves_holdings_and_candidates_empty_defaults(self):
        jp_state = {"start_date": "2026-01-01", "cash_jpy": 0.0, "lots": []}
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(config, "RSI_JP_HISTORY_CSV_PATH", Path(tmp) / "history.csv"), \
                 patch.object(config, "RSI_JP_TRADES_CSV_PATH", Path(tmp) / "trades.csv"), \
                 patch.object(config, "RSI_JP_DIVIDENDS_CSV_PATH", Path(tmp) / "dividends.csv"):
                block = report.build_rsi_jp_block(jp_state, {}, 0.0, [])
        self.assertEqual(block["candidates"], [])
        self.assertIsNone(block["candidates_as_of"])


class TestBuildDataJsonUnrealizedPnl(unittest.TestCase):
    """2026-10-09追加・Change2: 本体枠(build_data_json)の保有一覧に含み損益の金額・%が付くこと。"""

    def test_holdings_carry_unrealized_pnl_usd_and_pct(self):
        with tempfile.TemporaryDirectory() as tmp:
            trades_path = Path(tmp) / "trades.csv"
            history_path = Path(tmp) / "history.csv"
            trades_path.write_text(
                "date,action,ticker,shares,price,currency,amount_usd,fee_usd,rule,note,"
                "realized_pnl,realized_pnl_pct\r\n"
                "2026-09-01,BUY,GLD,10,100.0,USD,1000.0,1.0,entry,,,\r\n",
                encoding="utf-8",
            )
            history_path.write_text("date,nav_usd,bench_usd,diff_usd,diff_pct,cash_ratio\r\n", encoding="utf-8")
            state = {"holdings": {"GLD": 10.0}, "cash_usd": 0.0, "start_date": "2026-08-05", "mode": "normal"}
            market = {"GLD": TickerSnapshot(ticker="GLD", close=120.0, date="2026-10-09")}
            with patch.object(config, "TRADES_CSV_PATH", trades_path), \
                 patch.object(config, "HISTORY_CSV_PATH", history_path):
                data = report.build_data_json(
                    state, market, nav_usd=1200.0, bench_usd=1100.0, accepted_trades=[],
                    now_jst=dt.datetime(2026, 10, 9),
                )
        holding = data["holdings"][0]
        self.assertAlmostEqual(holding["unrealized_pnl_usd"], 200.0, places=2)
        self.assertAlmostEqual(holding["unrealized_pnl_pct"], 20.0, places=2)


if __name__ == "__main__":
    unittest.main()
