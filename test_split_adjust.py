"""株式分割の自動調整（2026-10-01追加）の単体テスト。

大将「分割は分割できちんと計算しないとね。」。2026-09-29に日本株RSI枠で3099(1:2)・9065(1:5)の
分割を暴落と誤認し、-8%損切りが誤発動した事故の再発防止。
実データ・moomoo接続・yfinance接続は一切使わない。
実行: python3 -m unittest test_split_adjust.py -v
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

import broker
import jp_market
import jp_rsi_daily
import rsi_daily
import rsi_strategy
from jp_market import JpSnapshot

JP = rsi_strategy.JP_RULES


def _lot(ticker="3099", shares=1300, entry_price=3211.0, avg_cost=3246.6923076923076, entry_date="2026-09-03"):
    lot = rsi_strategy.new_lot(ticker, f"{ticker}-{entry_date}-1", entry_date, shares, entry_price, 100)
    lot["avg_cost"] = avg_cost
    lot["total_invested_usd"] = shares * avg_cost
    return lot


class TestApplySplit(unittest.TestCase):
    def test_split_1_to_2(self):
        lot = _lot()
        new, applied = rsi_strategy.adjust_lot_for_splits(lot, [("2026-09-29", 2.0)], "2026-09-29")
        self.assertEqual(applied, [("2026-09-29", 2.0)])
        self.assertEqual(new["shares"], 2600)
        self.assertIsInstance(new["shares"], int)
        self.assertAlmostEqual(new["initial_entry_price"], 1605.5)
        self.assertAlmostEqual(new["avg_cost"], 3246.6923076923076 / 2)
        self.assertEqual(new["total_invested_usd"], lot["total_invested_usd"])  # 金額は不変
        self.assertEqual(new["splits_applied"], ["2026-09-29"])
        self.assertEqual(lot["shares"], 1300)  # 元のロットは変更しない

    def test_split_1_to_5(self):
        lot = _lot("9065", 300, 8064.0, 8064.0)
        new, _ = rsi_strategy.adjust_lot_for_splits(lot, [("2026-09-29", 5.0)], "2026-09-29")
        self.assertEqual(new["shares"], 1500)
        self.assertAlmostEqual(new["initial_entry_price"], 1612.8)
        self.assertAlmostEqual(new["avg_cost"], 1612.8)

    def test_base_shares_is_scaled_after_profit1(self):
        lot = {**_lot(), "profit1_taken": True, "base_shares": 1300, "shares": 650}
        new, _ = rsi_strategy.adjust_lot_for_splits(lot, [("2026-09-29", 2.0)], "2026-09-29")
        self.assertEqual(new["base_shares"], 2600)
        self.assertEqual(new["shares"], 1300)

    def test_idempotent_on_rerun(self):
        lot = _lot()
        once, _ = rsi_strategy.adjust_lot_for_splits(lot, [("2026-09-29", 2.0)], "2026-09-29")
        twice, applied = rsi_strategy.adjust_lot_for_splits(once, [("2026-09-29", 2.0)], "2026-09-29")
        self.assertEqual(applied, [])
        self.assertEqual(twice, once)
        later, applied = rsi_strategy.adjust_lot_for_splits(once, [("2026-09-29", 2.0)], "2026-10-01")
        self.assertEqual(applied, [])
        self.assertEqual(later["shares"], 2600)

    def test_splits_on_or_before_entry_date_and_future_are_ignored(self):
        lot = _lot()
        splits = [("2017-09-27", 0.2), ("2026-09-03", 2.0), ("2026-10-05", 3.0)]
        new, applied = rsi_strategy.adjust_lot_for_splits(lot, splits, "2026-09-30")
        self.assertEqual(applied, [])
        self.assertEqual(new["shares"], 1300)

    def test_closed_lot_is_untouched(self):
        lot = {**_lot(), "closed": True, "shares": 0}
        new, applied = rsi_strategy.adjust_lot_for_splits(lot, [("2026-09-29", 2.0)], "2026-09-29")
        self.assertEqual(applied, [])
        self.assertIs(new, lot)

    def test_stop_loss_not_triggered_after_split_with_flat_price(self):
        """3099の実例: 分割後の実価格1618円（分割前換算3236円＝ほぼ横ばい）では損切りしない。"""
        lot = _lot()
        self.assertIsNotNone(rsi_strategy.decide_stop_loss(lot, 1618.0, JP))  # 未調整だと誤発動（事故の再現）
        adjusted, _ = rsi_strategy.adjust_lot_for_splits(lot, [("2026-09-29", 2.0)], "2026-09-29")
        self.assertIsNone(rsi_strategy.decide_stop_loss(adjusted, 1618.0, JP))
        self.assertEqual(rsi_strategy.decide_pyramid_buys(adjusted, 1618.0, JP), [])
        self.assertEqual(rsi_strategy.decide_profit_takes(adjusted, 1618.0, "2026-09-29", 18, JP), [])


def _jp_state(lots, cash=310_000.0):
    return {"start_date": "2026-08-24", "cash_jpy": cash, "lots": lots, "last_processed_date": "2026-09-28"}


class TestRunJpAdjustsSplitsBeforeDecisions(unittest.TestCase):
    def _run(self, state, splits_side_effect):
        snaps = {"3099": JpSnapshot("3099", 1618.0, "2026-09-29"), "9065": JpSnapshot("9065", 1567.0, "2026-09-29")}
        with patch("jp_rsi_daily.jp_market.get_splits", side_effect=splits_side_effect), \
             patch("jp_rsi_daily.jp_market.get_snapshots", return_value=snaps), \
             patch("jp_rsi_daily.get_jp_candidates", return_value=[]), \
             patch("jp_rsi_daily.jp_lotsize.get_lot_sizes", return_value={}), \
             patch("jp_rsi_daily.jp_lotsize.get_company_tickers", return_value=set()), \
             patch("jp_rsi_daily.jp_rsi_ledger.append_trade_row") as mock_trade, \
             patch("jp_rsi_daily.jp_rsi_ledger.append_history_row"), \
             patch("jp_rsi_daily.jp_rsi_ledger.save_portfolio"):
            result = jp_rsi_daily.run_jp(state, "2026-09-29", dry_run=False)
        return result, mock_trade

    def test_no_stop_loss_and_nav_uses_adjusted_shares(self):
        state = _jp_state([_lot(), _lot("9065", 300, 8064.0, 8064.0)])
        splits = {"3099": [("2026-09-29", 2.0)], "9065": [("2017-09-27", 0.2), ("2026-09-29", 5.0)]}
        (new_state, trades, log_lines, nav, _), mock_trade = self._run(state, lambda t: splits[t])
        self.assertEqual(trades, [])
        mock_trade.assert_not_called()
        by_ticker = {lot["ticker"]: lot for lot in new_state["lots"]}
        self.assertEqual(by_ticker["3099"]["shares"], 2600)
        self.assertEqual(by_ticker["9065"]["shares"], 1500)
        self.assertFalse(by_ticker["3099"]["closed"])
        self.assertAlmostEqual(nav, 310_000.0 + 2600 * 1618.0 + 1500 * 1567.0)
        self.assertTrue(any("株式分割を反映" in line for line in log_lines))

    def test_split_fetch_failure_warns_and_continues(self):
        state = _jp_state([_lot()])
        with self.assertLogs("vs-sp500.jp_rsi_daily", level="WARNING") as cm:
            (new_state, _, _, _, _), _ = self._run(state, lambda t: None)
        self.assertTrue(any("分割情報が取得できず" in m for m in cm.output))
        self.assertEqual(new_state["last_processed_date"], "2026-09-29")  # 実行は止まらない


class TestJpMarketGetSplits(unittest.TestCase):
    def test_parses_yfinance_splits(self):
        idx = pd.DatetimeIndex(["2017-09-27", "2026-09-29"]).tz_localize("Asia/Tokyo")
        fake = type("T", (), {"splits": pd.Series([0.2, 5.0], index=idx)})
        with patch.object(jp_market.yf, "Ticker", return_value=fake):
            self.assertEqual(jp_market.get_splits("9065"), [("2017-09-27", 0.2), ("2026-09-29", 5.0)])

    def test_exception_returns_none(self):
        with patch.object(jp_market.yf, "Ticker", side_effect=RuntimeError("boom")):
            self.assertIsNone(jp_market.get_splits("9065"))


class _FakeQuoteCtx:
    """moomoo OpenQuoteContext.get_rehabの実機応答（2026-10-01確認）の形を模したもの。"""

    def __init__(self, *args, **kwargs):
        pass

    def get_rehab(self, code):
        nan = float("nan")
        rows = {
            "US.NVDA": [
                {"ex_div_date": "2024-06-10", "split_base": 1.0, "split_ert": 10.0, "join_base": nan, "join_ert": nan},
                {"ex_div_date": "2026-09-10", "split_base": nan, "split_ert": nan, "join_base": nan, "join_ert": nan},
            ],
            "US.GE": [
                {"ex_div_date": "2021-08-02", "split_base": nan, "split_ert": nan, "join_base": 8.0, "join_ert": 1.0},
            ],
        }
        if code not in rows:
            return -1, "unknown stock"
        return 0, pd.DataFrame(rows[code])

    def close(self):
        pass


class TestUsSplitsViaMoomoo(unittest.TestCase):
    def test_get_splits_parses_rehab_split_and_join(self):
        with patch("moomoo.OpenQuoteContext", _FakeQuoteCtx):
            result = broker.get_splits(["NVDA", "GE", "XXX"])
        self.assertEqual(result["NVDA"], [("2024-06-10", 10.0)])
        self.assertEqual(result["GE"], [("2021-08-02", 0.125)])
        self.assertNotIn("XXX", result)  # 個別失敗はその銘柄だけ欠ける

    def test_us_run_adjusts_lot_with_mocked_moomoo(self):
        lot = rsi_strategy.new_lot("NVDA", "NVDA-2024-06-01-1", "2024-06-01", 10, 1200.0)
        state = {"lots": [lot]}
        log_lines: list[str] = []
        with patch("moomoo.OpenQuoteContext", _FakeQuoteCtx):
            rsi_daily.adjust_lots_for_splits(state, "2024-06-10", log_lines)
        adjusted = state["lots"][0]
        self.assertEqual(adjusted["shares"], 100)
        self.assertAlmostEqual(adjusted["initial_entry_price"], 120.0)
        self.assertEqual(adjusted["splits_applied"], ["2024-06-10"])
        self.assertIsNone(rsi_strategy.decide_stop_loss(adjusted, 121.0))  # 横ばいなら損切りしない
        with patch("moomoo.OpenQuoteContext", _FakeQuoteCtx):
            rsi_daily.adjust_lots_for_splits(state, "2024-06-11", log_lines)  # 再実行
        self.assertEqual(state["lots"][0]["shares"], 100)

    def test_us_split_fetch_failure_warns_and_continues(self):
        lot = rsi_strategy.new_lot("NVDA", "NVDA-2024-06-01-1", "2024-06-01", 10, 1200.0)
        state = {"lots": [lot]}
        log_lines: list[str] = []
        with patch("rsi_daily.broker.get_splits", return_value=None), \
             self.assertLogs("vs-sp500.rsi_daily", level="WARNING"):
            rsi_daily.adjust_lots_for_splits(state, "2024-06-10", log_lines)
        self.assertEqual(state["lots"][0]["shares"], 10)


if __name__ == "__main__":
    unittest.main()
