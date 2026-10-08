"""report.py: ダッシュボード「今夜の候補」・保有一覧の規模(大/中/小)表示の単体テスト（2026-10-08追加）。

build_rsi_block/build_rsi_jp_blockのdashboard引数（rsi_daily/jp_rsi_daily の
build_dashboard_candidates*()が返す辞書）が、保有一覧のsize_label/market_cap と
候補一覧(candidates)に正しく反映されることを検証する。実データ・moomoo接続は使わない。
history.csv/trades.csv/dividends.csvは実ファイルを読ませず、一時ディレクトリに差し替える。
実行: python3 -m unittest test_dashboard_report.py -v
"""
from __future__ import annotations

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

    def test_none_dashboard_leaves_holdings_and_candidates_empty_defaults(self):
        jp_state = {"start_date": "2026-01-01", "cash_jpy": 0.0, "lots": []}
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(config, "RSI_JP_HISTORY_CSV_PATH", Path(tmp) / "history.csv"), \
                 patch.object(config, "RSI_JP_TRADES_CSV_PATH", Path(tmp) / "trades.csv"), \
                 patch.object(config, "RSI_JP_DIVIDENDS_CSV_PATH", Path(tmp) / "dividends.csv"):
                block = report.build_rsi_jp_block(jp_state, {}, 0.0, [])
        self.assertEqual(block["candidates"], [])
        self.assertIsNone(block["candidates_as_of"])


if __name__ == "__main__":
    unittest.main()
