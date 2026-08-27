"""日本株RSI枠の単体テスト。

rsi_strategy.py（米国RSI-32枠と共用のルールエンジン）にJP_RULESとlot_sizeを渡した場合の
挙動と、jp_rsi_daily.pyのJP固有の純粋関数（1単元予算超過・lot_size不明のスキップ）を検証する。
実データ・moomoo/yfinance接続は使わない。
実行: python3 -m unittest test_jp_rsi.py -v
"""
from __future__ import annotations

import datetime as dt
import unittest
from unittest import mock

import pandas as pd

import config
import jp_market
import jp_rsi_daily
import rsi_strategy as rs


class TestJpEntryCandidateFiltering(unittest.TestCase):
    """検証4: 1単元が予算(300万円)を超える値がさ株は見送らず1単元だけ買うこと（2026-08-27改訂）。
    lot_sizeが100以外の銘柄（例: 1）でも正しく候補に含まれること。"""

    def test_a_unit_price_over_budget_buys_one_lot_only(self):
        # ファストリテイリング 9983 実例: 72,840円×100株=728万円 > 300万円 → 1単元(100株)だけ買う
        candidates = [{"ticker": "9983", "rsi14": 33.7, "price": 72840.0, "market_cap": 2.3e13}]
        lot_sizes = {"9983": 100}

        allowed, no_lotsize = jp_rsi_daily.build_entry_candidates(candidates, lot_sizes)

        self.assertEqual([c["ticker"] for c in allowed], ["9983"])
        self.assertEqual(allowed[0]["lot_size"], 100)
        self.assertEqual(no_lotsize, [])

        selected = rs.select_entries_within_cash(allowed, available_cash=10_000_000.0, rules=rs.JP_RULES)
        self.assertEqual(selected[0]["qty"], 100)  # 1単元(100株)のみ。728.4万円 > 300万円だが最低1単元は買う

    def test_a2_unit_price_over_budget_and_cash_insufficient_is_not_bought(self):
        # 同じくファストリ。現金が1単元(728.4万円)に満たない場合は見送ること。
        candidates = [{"ticker": "9983", "rsi14": 33.7, "price": 72840.0, "market_cap": 2.3e13}]
        lot_sizes = {"9983": 100}
        allowed, _ = jp_rsi_daily.build_entry_candidates(candidates, lot_sizes)

        selected = rs.select_entries_within_cash(allowed, available_cash=5_000_000.0, rules=rs.JP_RULES)
        self.assertEqual(selected, [])

    def test_b_lot_size_other_than_100_is_used_correctly(self):
        # lot_size=1の会社銘柄例（額面が小さくlot_size=1の銘柄はJ-REIT以外にも存在しうる）
        candidates = [{"ticker": "1234", "rsi14": 27.9, "price": 99200.0, "market_cap": 3.5e10}]
        lot_sizes = {"1234": 1}

        allowed, no_lotsize = jp_rsi_daily.build_entry_candidates(candidates, lot_sizes)

        self.assertEqual([c["ticker"] for c in allowed], ["1234"])
        self.assertEqual(allowed[0]["lot_size"], 1)
        self.assertEqual(no_lotsize, [])

        # select_entries_within_cashで単元(1株)単位に切り捨てて計算されること
        selected = rs.select_entries_within_cash(allowed, available_cash=10_000_000.0, rules=rs.JP_RULES)
        self.assertEqual(selected[0]["qty"], 30)  # floor(3,000,000 / 99200) = 30（lot_size=1なので端数なし）

    def test_unknown_lot_size_is_skipped_and_not_guessed(self):
        candidates = [{"ticker": "9999", "rsi14": 30.0, "price": 1000.0, "market_cap": 5e10}]

        allowed, no_lotsize = jp_rsi_daily.build_entry_candidates(candidates, {})

        self.assertEqual(allowed, [])
        self.assertEqual(no_lotsize, ["9999"])

    def test_lot_size_100_rounds_down_to_unit_multiple(self):
        # 6367 実例: lot_size=100、3,000,000円で 20855円 → floor(3000000/20855)=143 → 100株単位に切り捨てで100株
        candidates = [{"ticker": "6367", "rsi14": 28.1, "price": 20855.0, "market_cap": 6.1e12}]
        lot_sizes = {"6367": 100}
        allowed, _ = jp_rsi_daily.build_entry_candidates(candidates, lot_sizes)
        selected = rs.select_entries_within_cash(allowed, available_cash=10_000_000.0, rules=rs.JP_RULES)
        self.assertEqual(selected[0]["qty"], 100)

    def test_cheap_stock_buys_ten_units(self):
        # 検証4: 1単元が30万円の銘柄（lot_size=100・株価3,000円）で10単元(1000株)買われること
        candidates = [{"ticker": "5555", "rsi14": 29.0, "price": 3000.0, "market_cap": 5e10}]
        lot_sizes = {"5555": 100}
        allowed, _ = jp_rsi_daily.build_entry_candidates(candidates, lot_sizes)
        selected = rs.select_entries_within_cash(allowed, available_cash=10_000_000.0, rules=rs.JP_RULES)
        self.assertEqual(selected[0]["qty"], 1000)  # floor(3,000,000 / 3000) = 1000株 = 10単元


class TestJpCompanyOnlyFiltering(unittest.TestCase):
    """検証2: 「会社の株のみ」ルール（REIT除外）。moomoo実機確認値に基づく。"""

    def test_reit_tickers_are_excluded(self):
        # 3455(ヘルスケア&メディカル投資法人)・2979(SOSiLA物流リート)はSTOCK区分に無い実機確認済み
        candidates = [
            {"ticker": "3455", "rsi14": 27.9, "price": 99200.0},
            {"ticker": "2979", "rsi14": 30.0, "price": 102600.0},
        ]
        company_tickers = {"6367", "9983", "7532", "3905"}

        allowed, excluded = jp_rsi_daily.filter_non_company_entries(candidates, company_tickers)

        self.assertEqual(allowed, [])
        self.assertEqual(sorted(excluded), ["2979", "3455"])

    def test_company_tickers_are_allowed(self):
        # 6367(ダイキン)・9983(ファストリ)・7532(パンパシ)・3905(データセクション)はSTOCK区分にある実機確認済み
        candidates = [
            {"ticker": "6367", "rsi14": 28.1, "price": 20855.0},
            {"ticker": "9983", "rsi14": 33.7, "price": 72840.0},
            {"ticker": "7532", "rsi14": 35.0, "price": 810.7},
            {"ticker": "3905", "rsi14": 33.6, "price": 1662.0},
        ]
        company_tickers = {"6367", "9983", "7532", "3905"}

        allowed, excluded = jp_rsi_daily.filter_non_company_entries(candidates, company_tickers)

        self.assertEqual([c["ticker"] for c in allowed], ["6367", "9983", "7532", "3905"])
        self.assertEqual(excluded, [])


class TestJpRulesShareUsBehaviorRatios(unittest.TestCase):
    """検証2c: 買い増し・利確・伸ばす玉・例外条項・同一銘柄1ロット制限が米国版と同じ挙動になること
    （rsi_strategy.pyの同じ関数群にJP_RULESを渡すだけで実現しているため、金額の比率だけが変わり
    判定ロジック自体は米国RSI-32枠のテストと同一パターンで検証する）。"""

    def test_pyramid_three_stages_use_jp_amounts_and_lot_size(self):
        # entry: 300万円 / 単価3000円 / lot_size=100 → floor(3,000,000/3000/100)*100 = 1000株
        lot = rs.new_lot("TST", "TST-1", "2026-01-05", filled_qty=1000, fill_price=3000.0, lot_size=100)
        self.assertEqual(lot["total_invested_usd"], 3_000_000.0)
        self.assertEqual(lot["lot_size"], 100)

        lot, trades1 = rs.simulate_lot_day(lot, 3075.0, "2026-01-06", rules=rs.JP_RULES)  # +2.5%
        self.assertEqual([t["kind"] for t in trades1], ["pyramid1"])
        # floor(1,500,000/3075/100)*100 = floor(487.8/100)*100 = 400
        self.assertEqual(trades1[0]["filled_qty"], 400)
        self.assertTrue(lot["pyramid_done"][0])

        lot, trades2 = rs.simulate_lot_day(lot, 3150.0, "2026-01-07", rules=rs.JP_RULES)  # +5.0%
        self.assertEqual([t["kind"] for t in trades2], ["pyramid2"])
        # floor(750,000/3150/100)*100 = floor(238.09/100)*100 = 200
        self.assertEqual(trades2[0]["filled_qty"], 200)

    def _lot_after_pyramids(self):
        lot = rs.new_lot("TST", "TST-1", "2026-01-05", filled_qty=1000, fill_price=3000.0, lot_size=100)
        lot, _ = rs.simulate_lot_day(lot, 3075.0, "2026-01-06", rules=rs.JP_RULES)   # +2.5%
        lot, _ = rs.simulate_lot_day(lot, 3150.0, "2026-01-07", rules=rs.JP_RULES)   # +5.0%
        lot, _ = rs.simulate_lot_day(lot, 3225.0, "2026-01-08", rules=rs.JP_RULES)   # +7.5%
        return lot

    def test_profit_taking_leaves_25pct_runner_with_jp_rules(self):
        lot = self._lot_after_pyramids()
        base_shares = lot["shares"]
        avg_cost = lot["avg_cost"]

        profit1_price = avg_cost * 1.20 * 1.001
        lot, trades = rs.simulate_lot_day(lot, profit1_price, "2026-03-02", rules=rs.JP_RULES)
        self.assertEqual([t["kind"] for t in trades], ["profit1"])
        expected_qty1 = int(base_shares * 0.5)
        self.assertEqual(trades[0]["filled_qty"], expected_qty1)
        self.assertTrue(lot["profit1_taken"])

        profit2_price = avg_cost * 1.25 * 1.001
        lot, trades = rs.simulate_lot_day(lot, profit2_price, "2026-03-03", rules=rs.JP_RULES)
        self.assertEqual([t["kind"] for t in trades], ["profit2"])
        expected_qty2 = int(base_shares * 0.25)
        self.assertEqual(trades[0]["filled_qty"], expected_qty2)
        remaining = lot["shares"]
        self.assertEqual(remaining, base_shares - expected_qty1 - expected_qty2)
        self.assertAlmostEqual(remaining, base_shares * 0.25, delta=1)  # 25%の伸ばす玉が残る

        # 伸ばす玉はどれだけ上がっても追加売却されない
        lot, trades = rs.simulate_lot_day(lot, avg_cost * 3.0, "2026-03-04", rules=rs.JP_RULES)
        self.assertEqual(trades, [])
        self.assertEqual(lot["shares"], remaining)

    def test_no_stop_loss_with_jp_rules(self):
        """米国版と同じく損切りルールは無い（-30%でも何も起きない）。"""
        lot = rs.new_lot("TST", "TST-1", "2026-01-05", filled_qty=1000, fill_price=3000.0, lot_size=100)
        lot, trades = rs.simulate_lot_day(lot, 2100.0, "2026-01-06", rules=rs.JP_RULES)  # -30%
        self.assertEqual(trades, [])
        self.assertFalse(lot["closed"])

    def test_exception_15day_window_with_jp_rules(self):
        entry_date = "2026-01-05"
        lot = rs.new_lot("TST", "TST-1", entry_date, filled_qty=1000, fill_price=3000.0, lot_size=100)
        lot, _ = rs.simulate_lot_day(lot, 3075.0, "2026-01-06", rules=rs.JP_RULES)
        lot, _ = rs.simulate_lot_day(lot, 3150.0, "2026-01-07", rules=rs.JP_RULES)
        lot, _ = rs.simulate_lot_day(lot, 3225.0, "2026-01-08", rules=rs.JP_RULES)
        avg_cost = lot["avg_cost"]
        day10 = "2026-01-19"
        self.assertEqual(rs.business_days_since(entry_date, day10), 10)

        price = avg_cost * 1.20 * 1.001
        lot, trades = rs.simulate_lot_day(lot, price, day10, rules=rs.JP_RULES)
        self.assertEqual([t["kind"] for t in trades], ["exception_trigger"])
        self.assertTrue(lot["exception_active"])
        expected_deadline = (dt.date.fromisoformat(entry_date) + dt.timedelta(days=56)).isoformat()
        self.assertEqual(lot["exception_deadline_date"], expected_deadline)
        self.assertFalse(lot["profit1_taken"])

    def test_same_ticker_one_lot_limit_reuses_shared_function(self):
        """filter_blocked_entriesは通貨非依存の共有関数のため、JP枠でも米国枠と同じ挙動になる。"""
        lot = rs.new_lot("6367", "6367-1", "2026-08-18", filled_qty=100, fill_price=20855.0, lot_size=100)
        candidates = [{"ticker": "6367", "rsi14": 25.0, "price": 20000.0}]

        allowed, blocked = rs.filter_blocked_entries(candidates, [lot])

        self.assertEqual(allowed, [])
        self.assertEqual(blocked, ["6367"])


class TestJpCashPriority(unittest.TestCase):
    """検証2d: 現金不足時にRSIが低い順で選ばれること（JP_RULES・lot_size込み）。"""

    def test_lowest_rsi_selected_first_when_cash_limited(self):
        candidates = [
            {"ticker": "A", "rsi14": 28.0, "price": 1000.0, "lot_size": 100},
            {"ticker": "B", "rsi14": 15.0, "price": 1000.0, "lot_size": 100},
            {"ticker": "C", "rsi14": 25.0, "price": 1000.0, "lot_size": 100},
        ]
        # 1件300万円 x 1件分だけ現金がある(3件中1件しか買えない)
        available_cash = config.RSI_JP_ENTRY_AMOUNT_JPY
        selected = rs.select_entries_within_cash(candidates, available_cash, rules=rs.JP_RULES)
        self.assertEqual([c["ticker"] for c in selected], ["B"])  # RSI最小


class TestGetSnapshotsNanCloseFallback(unittest.TestCase):
    """診断: yfinanceのhistory(period='5d')は日本株(.T)について深夜、Yahooの複数日レンジ
    エンドポイントの反映遅延により最新営業日の行をClose=NaNで返すことがある
    （2026-08-26 02:00 JST実測）。同時刻のhistory(period='1d')では正常値が返ることを
    確認済みのため、そのフォールバック挙動をyf.Tickerをモックして検証する。"""

    @staticmethod
    def _df(dates, closes):
        index = pd.DatetimeIndex([pd.Timestamp(d) for d in dates])
        return pd.DataFrame({"Close": closes}, index=index)

    @staticmethod
    def _empty_df():
        return pd.DataFrame({"Close": pd.Series(dtype="float64")})

    @staticmethod
    def _fake_ticker_factory(hist_5d, hist_1d):
        class _FakeTicker:
            def __init__(self, ticker):
                self.ticker = ticker

            def history(self, period=None, auto_adjust=None):
                if period == "5d":
                    return hist_5d
                if period == "1d":
                    return hist_1d
                raise AssertionError(f"unexpected period: {period!r}")

        return _FakeTicker

    def test_a_5d_latest_nan_1d_has_newer_valid_close(self):
        # 5dの最終行(8/25)がNaN・1dに同日8/25の有効な終値がある → 1dの終値・日付を採用
        hist_5d = self._df(["2026-08-24", "2026-08-25"], [100.0, float("nan")])
        hist_1d = self._df(["2026-08-25"], [105.0])
        with mock.patch.object(jp_market.yf, "Ticker", self._fake_ticker_factory(hist_5d, hist_1d)):
            result = jp_market.get_snapshots(["6367"])

        self.assertIn("6367", result)
        self.assertEqual(result["6367"].close, 105.0)
        self.assertEqual(result["6367"].date, "2026-08-25")

    def test_b_5d_latest_nan_1d_empty_falls_back_to_5d_last_valid(self):
        # 5dの最終行(8/25)がNaN・1dは空 → 5dの直近の有効な終値(8/24)を採用
        hist_5d = self._df(["2026-08-24", "2026-08-25"], [100.0, float("nan")])
        hist_1d = self._empty_df()
        with mock.patch.object(jp_market.yf, "Ticker", self._fake_ticker_factory(hist_5d, hist_1d)):
            result = jp_market.get_snapshots(["6367"])

        self.assertIn("6367", result)
        self.assertEqual(result["6367"].close, 100.0)
        self.assertEqual(result["6367"].date, "2026-08-24")

    def test_c_5d_all_nan_and_1d_empty_is_skipped(self):
        # 5dが全行NaN・1dも空 → 有効な終値が無いのでこの銘柄は結果に含めない
        hist_5d = self._df(["2026-08-24", "2026-08-25"], [float("nan"), float("nan")])
        hist_1d = self._empty_df()
        with mock.patch.object(jp_market.yf, "Ticker", self._fake_ticker_factory(hist_5d, hist_1d)):
            result = jp_market.get_snapshots(["6367"])

        self.assertNotIn("6367", result)
        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
