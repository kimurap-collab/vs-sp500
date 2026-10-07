"""日本株RSI枠の単体テスト。

rsi_strategy.py（米国RSI-32枠と共用のルールエンジン）にJP_RULESとlot_sizeを渡した場合の
挙動と、jp_rsi_daily.pyのJP固有の純粋関数（1単元予算超過・lot_size不明のスキップ）を検証する。
実データ・moomoo/yfinance接続は使わない。
実行: python3 -m unittest test_jp_rsi.py -v
"""
from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import patch

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

    def test_stop_loss_triggers_and_closes_lot_with_jp_rules(self):
        """JP枠も米国枠と同じ-8%損切りルールを共用する（2026-09-28復活。大将「日本枠にも適用」）。
        台帳のみの仮想売買（moomoo発注なし）でロットが閉じること。"""
        lot = rs.new_lot("TST", "TST-1", "2026-01-05", filled_qty=1000, fill_price=3000.0, lot_size=100)
        stop_price = 3000.0 * (1 + config.RSI_STOP_LOSS_PCT)  # -8% = 2760.0
        lot, trades = rs.simulate_lot_day(lot, stop_price, "2026-01-06", rules=rs.JP_RULES)
        self.assertEqual([t["kind"] for t in trades], ["stop_loss"])
        self.assertEqual(trades[0]["filled_qty"], 1000)
        self.assertEqual(lot["shares"], 0)
        self.assertTrue(lot["closed"])
        self.assertEqual(lot["closed_reason"], "stop_loss")

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

    def test_stop_loss_reentry_block_reuses_shared_function(self):
        """filter_stop_loss_reentriesも通貨非依存の共有関数のため、JP枠（円建て）でも
        米国枠と同じ挙動になる（2026-10-07追加）。"""
        history = {"5334": {"date": "2026-09-28", "price": 3829.0}}  # 分割調整済み(7658/2)
        candidates = [{"ticker": "5334", "price": 3300.0}]  # 3829*0.85=3254.65を上回る

        allowed, blocked = rs.filter_stop_loss_reentries(candidates, history, "2026-10-07")

        self.assertEqual(allowed, [])
        self.assertEqual([b["ticker"] for b in blocked], ["5334"])

        allowed2, blocked2 = rs.filter_stop_loss_reentries(candidates, history, "2026-10-12")
        self.assertEqual([c["ticker"] for c in allowed2], ["5334"])
        self.assertEqual(blocked2, [])


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


# ---------------------------------------------------------------------------
# スワップ売却（JP枠。2026-10-07追加。SPEC_RSI30.md「2026-10-07改訂2」参照）
# ---------------------------------------------------------------------------

def _jp_lot(ticker, lot_id, avg_cost, shares, lot_size=100, profit1_taken=False, profit2_taken=False):
    return {
        "lot_id": lot_id, "ticker": ticker, "name": None,
        "initial_entry_date": "2026-09-01", "initial_entry_price": avg_cost,
        "pyramid_done": [False, False, False], "shares": shares, "lot_size": lot_size,
        "total_invested_usd": shares * avg_cost, "avg_cost": avg_cost,
        "profit1_taken": profit1_taken, "profit2_taken": profit2_taken, "base_shares": None,
        "exception_active": False, "exception_deadline_date": None,
        "closed": False, "closed_reason": None, "closed_date": None,
    }


def _jp_state(**overrides):
    base = {"start_date": "2026-08-24", "cash_jpy": 0.0, "lots": [], "last_processed_date": None}
    base.update(overrides)
    return base


class TestGetMarketCapTiersJp(unittest.TestCase):
    """JP枠は時価総額Tierの符号を米国枠と反転する（小型+1/大型-1）。データ源はfrozen候補の
    market_cap（moomooスクリーナー由来）を優先し、保有銘柄で候補に無い分だけyfinanceで補う
    （2026-10-07追加）。"""

    def test_small_cap_gets_plus_one_large_cap_gets_minus_one(self):
        raw_candidates = [
            {"ticker": "SMALL", "rsi14": 20.0, "price": 1000.0, "market_cap": 1.0e11},
            {"ticker": "MID", "rsi14": 25.0, "price": 1000.0, "market_cap": 5.0e11},
            {"ticker": "LARGE", "rsi14": 30.0, "price": 1000.0, "market_cap": 9.0e11},
        ]
        tiers = jp_rsi_daily.get_market_cap_tiers_jp(raw_candidates, held_tickers=[], log_lines=[])
        self.assertEqual(tiers["SMALL"], 1)   # 米国枠なら-1になるところがJPは+1
        self.assertEqual(tiers["LARGE"], -1)  # 米国枠なら+1になるところがJPは-1

    def test_held_ticker_missing_from_candidates_falls_back_to_yfinance(self):
        raw_candidates = [{"ticker": "SMALL", "rsi14": 20.0, "price": 1000.0, "market_cap": 1.0e11}]
        with patch("jp_rsi_daily.jp_market.get_market_caps", return_value={"HELD": 9.0e11}) as mock_caps:
            tiers = jp_rsi_daily.get_market_cap_tiers_jp(raw_candidates, held_tickers=["HELD"], log_lines=[])

        mock_caps.assert_called_once_with(["HELD"])
        self.assertEqual(tiers["HELD"], -1)  # 大型株なのでJPでは-1

    def test_no_caps_available_returns_empty_dict_without_crashing(self):
        with patch("jp_rsi_daily.jp_market.get_market_caps", return_value={}):
            tiers = jp_rsi_daily.get_market_cap_tiers_jp([], held_tickers=["X"], log_lines=[])
        self.assertEqual(tiers, {})


class TestSectorMapJpMonthlyCaching(unittest.TestCase):
    def test_cache_reused_within_same_month(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "sector_map.json"
            with patch.object(config, "RSI_JP_SWAP_SECTOR_MAP_PATH", fake_path), \
                 patch("jp_rsi_daily.jp_market.get_info", return_value={"industry": "Semiconductors"}) as mock_info:
                first = jp_rsi_daily.get_sector_map_jp(["6758"], "2026-10-07", dry_run=False, log_lines=[])
                second = jp_rsi_daily.get_sector_map_jp(["6758"], "2026-10-20", dry_run=False, log_lines=[])

            mock_info.assert_called_once()  # 同じ月の2回目は呼ばない
            self.assertEqual(first, second)
            self.assertEqual(first["6758"], "1625")
            self.assertTrue(fake_path.exists())

    def test_dry_run_does_not_write_cache_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "sector_map.json"
            with patch.object(config, "RSI_JP_SWAP_SECTOR_MAP_PATH", fake_path), \
                 patch("jp_rsi_daily.jp_market.get_info", return_value={"industry": "Semiconductors"}):
                result = jp_rsi_daily.get_sector_map_jp(["6758"], "2026-10-07", dry_run=True, log_lines=[])

            self.assertEqual(result["6758"], "1625")
            self.assertFalse(fake_path.exists())  # dry-runはledger/配下を一切変更しない


class TestSectorTiersJpMonthlyCaching(unittest.TestCase):
    def test_cache_reused_within_same_month(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "sector_tiers.json"
            returns = {code: 0.01 * i for i, code in enumerate(config.RSI_JP_SWAP_SECTOR_ETFS)}
            with patch.object(config, "RSI_JP_SWAP_SECTOR_TIERS_PATH", fake_path), \
                 patch("jp_rsi_daily.jp_market.get_sector_etf_returns", return_value=returns) as mock_returns:
                first = jp_rsi_daily.get_sector_tiers_jp("2026-10-07", dry_run=False, log_lines=[])
                second = jp_rsi_daily.get_sector_tiers_jp("2026-10-20", dry_run=False, log_lines=[])

            mock_returns.assert_called_once()
            self.assertEqual(first, second)
            # 17本 → 6/6/5（divmod(17,3)=(5,2)でsizes=[6,6,5]）
            self.assertEqual(sum(1 for v in first.values() if v == 1), 6)
            self.assertEqual(sum(1 for v in first.values() if v == 0), 6)
            self.assertEqual(sum(1 for v in first.values() if v == -1), 5)

    def test_insufficient_etf_returns_falls_back_without_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "sector_tiers.json"
            with patch.object(config, "RSI_JP_SWAP_SECTOR_TIERS_PATH", fake_path), \
                 patch("jp_rsi_daily.jp_market.get_sector_etf_returns", return_value={"1617": 0.01}):
                result = jp_rsi_daily.get_sector_tiers_jp("2026-10-07", dry_run=False, log_lines=[])

            self.assertEqual(result, {})
            self.assertFalse(fake_path.exists())


class TestRunSwapsJp(unittest.TestCase):
    """JP枠のスワップ実行（台帳のみの仮想売買）。スコア計算(_compute_swap_scores_jp)は
    固定値へ差し替え、rsi_strategy.decide_swaps以降の挙動だけを検証する
    （スコア計算そのものは米国枠と共用のためtest_rsi_strategy.pyで検証済み）。"""

    def test_runner_lot_is_never_sold_even_with_deepest_loss(self):
        runner = _jp_lot("RUNNER", "RUNNER-1", avg_cost=1000.0, shares=100,
                          profit1_taken=True, profit2_taken=True)
        seller = _jp_lot("SELLER", "SELLER-1", avg_cost=1000.0, shares=100)
        state = _jp_state(lots=[runner, seller], cash_jpy=0.0)
        unfunded = [{"ticker": "BUY", "rsi14": 10.0, "price": 500.0, "qty": 100,
                     "cost": 50_000.0, "lot_size": 100, "name": None}]
        market_prices = {"RUNNER": 400.0, "SELLER": 500.0}  # どちらも含み損だがRUNNERは伸ばす玉

        with patch("jp_rsi_daily._compute_swap_scores_jp", return_value={"BUY": 2, "SELLER": -1, "RUNNER": -1}), \
             patch("jp_rsi_daily.jp_rsi_ledger.append_trade_row"):
            accepted = jp_rsi_daily._run_swaps_jp(
                state, unfunded, raw_candidates=[], market_prices=market_prices,
                trading_date="2026-10-07", log_lines=[],
            )

        sold_tickers = {t["ticker"] for t in accepted if t["action"] == "SELL"}
        self.assertEqual(sold_tickers, {"SELLER"})
        self.assertNotIn("RUNNER", sold_tickers)
        self.assertFalse(state["lots"][0]["closed"])  # RUNNERは無傷のまま

    def test_multi_sell_when_one_lot_is_not_enough(self):
        seller_a = _jp_lot("A", "A-1", avg_cost=1000.0, shares=100)
        seller_b = _jp_lot("B", "B-1", avg_cost=1000.0, shares=100)
        state = _jp_state(lots=[seller_a, seller_b], cash_jpy=0.0)
        unfunded = [{"ticker": "BUY", "rsi14": 10.0, "price": 900.0, "qty": 100,
                     "cost": 90_000.0, "lot_size": 100, "name": None}]
        market_prices = {"A": 450.0, "B": 480.0}  # 各45,000円・48,000円の売却見込み。2件必要

        with patch("jp_rsi_daily._compute_swap_scores_jp", return_value={"BUY": 2, "A": -1, "B": -1}), \
             patch("jp_rsi_daily.jp_rsi_ledger.append_trade_row"):
            accepted = jp_rsi_daily._run_swaps_jp(
                state, unfunded, raw_candidates=[], market_prices=market_prices,
                trading_date="2026-10-07", log_lines=[],
            )

        sold = [t for t in accepted if t["action"] == "SELL"]
        self.assertEqual({t["ticker"] for t in sold}, {"A", "B"})
        bought = [t for t in accepted if t["action"] == "BUY"]
        self.assertEqual(len(bought), 1)
        self.assertEqual(bought[0]["ticker"], "BUY")
        self.assertEqual(bought[0]["rule"], "entry")  # 買いは通常のentryルール名のまま
        for row in sold:
            self.assertEqual(row["rule"], "swap")

    def test_strict_score_condition_blocks_equal_or_higher_scored_sells(self):
        # 売却候補のスコアが買い候補と同点のため、スワップは成立しない
        seller = _jp_lot("SELLER", "SELLER-1", avg_cost=1000.0, shares=100)
        state = _jp_state(lots=[seller], cash_jpy=0.0)
        unfunded = [{"ticker": "BUY", "rsi14": 10.0, "price": 500.0, "qty": 100,
                     "cost": 50_000.0, "lot_size": 100, "name": None}]
        market_prices = {"SELLER": 500.0}

        with patch("jp_rsi_daily._compute_swap_scores_jp", return_value={"BUY": 0, "SELLER": 0}), \
             patch("jp_rsi_daily.jp_rsi_ledger.append_trade_row") as mock_append:
            accepted = jp_rsi_daily._run_swaps_jp(
                state, unfunded, raw_candidates=[], market_prices=market_prices,
                trading_date="2026-10-07", log_lines=[],
            )

        self.assertEqual(accepted, [])
        mock_append.assert_not_called()
        self.assertFalse(state["lots"][0]["closed"])

    def test_highest_score_unfunded_candidate_is_resolved_first(self):
        seller = _jp_lot("SELLER", "SELLER-1", avg_cost=1000.0, shares=100)
        state = _jp_state(lots=[seller], cash_jpy=0.0)
        # LOW(score=0)はSELLER(score=-1)一本では賄えず、SELLERがHIGHに使われた後は
        # 売却候補が尽きるため成立しない。HIGH(score=2)はスコア降順で先に判定されるため成立する。
        unfunded = [
            {"ticker": "LOW", "rsi14": 15.0, "price": 500.0, "qty": 50, "cost": 25_000.0,
             "lot_size": 100, "name": None},
            {"ticker": "HIGH", "rsi14": 20.0, "price": 500.0, "qty": 100, "cost": 50_000.0,
             "lot_size": 100, "name": None},
        ]
        market_prices = {"SELLER": 500.0}

        with patch("jp_rsi_daily._compute_swap_scores_jp",
                   return_value={"LOW": 0, "HIGH": 2, "SELLER": -1}), \
             patch("jp_rsi_daily.jp_rsi_ledger.append_trade_row"):
            accepted = jp_rsi_daily._run_swaps_jp(
                state, unfunded, raw_candidates=[], market_prices=market_prices,
                trading_date="2026-10-07", log_lines=[],
            )

        bought = [t["ticker"] for t in accepted if t["action"] == "BUY"]
        self.assertEqual(bought, ["HIGH"])

    def test_ledger_only_no_pending_orders_tracked(self):
        """JP枠は発注が無いため、約定結果待ちのpending_orders概念が一切登場しないこと。"""
        seller = _jp_lot("SELLER", "SELLER-1", avg_cost=1000.0, shares=100)
        state = _jp_state(lots=[seller], cash_jpy=0.0)
        unfunded = [{"ticker": "BUY", "rsi14": 10.0, "price": 500.0, "qty": 100,
                     "cost": 50_000.0, "lot_size": 100, "name": None}]
        market_prices = {"SELLER": 500.0}

        with patch("jp_rsi_daily._compute_swap_scores_jp", return_value={"BUY": 2, "SELLER": -1}), \
             patch("jp_rsi_daily.jp_rsi_ledger.append_trade_row"):
            accepted = jp_rsi_daily._run_swaps_jp(
                state, unfunded, raw_candidates=[], market_prices=market_prices,
                trading_date="2026-10-07", log_lines=[],
            )

        self.assertNotIn("pending_orders", state)
        self.assertEqual(len(accepted), 2)
        self.assertEqual(state["cash_jpy"], 0.0)  # 売却50,000円を得て同額を即買付


class TestSwapSellTriggersJpReentryRule(unittest.TestCase):
    """スワップ売却も損切りと同じ15%/10営業日の再エントリー制限の対象になること（2026-10-07改訂2。
    rsi_strategy.STOP_LOSS_LIKE_RULESは米国枠と共用のため"swap"は既に含まれている。JP枠の
    trade_rowでrule="swap"が記録されれば自動的に対象になることを確認する）。"""

    def test_swap_trade_row_blocks_reentry_like_stop_loss(self):
        seller = _jp_lot("SELLER", "SELLER-1", avg_cost=1000.0, shares=100)
        state = _jp_state(lots=[seller], cash_jpy=0.0)
        unfunded = [{"ticker": "BUY", "rsi14": 10.0, "price": 500.0, "qty": 100,
                     "cost": 50_000.0, "lot_size": 100, "name": None}]
        market_prices = {"SELLER": 500.0}

        with patch("jp_rsi_daily._compute_swap_scores_jp", return_value={"BUY": 2, "SELLER": -1}), \
             patch("jp_rsi_daily.jp_rsi_ledger.append_trade_row"):
            accepted = jp_rsi_daily._run_swaps_jp(
                state, unfunded, raw_candidates=[], market_prices=market_prices,
                trading_date="2026-10-07", log_lines=[],
            )

        sell_row = next(t for t in accepted if t["action"] == "SELL")
        self.assertEqual(sell_row["rule"], "swap")

        history = rs.latest_rule_closures([sell_row])
        self.assertIn("SELLER", history)

        candidates = [{"ticker": "SELLER", "price": 500.0}]  # 500 > 500*0.85なのでまだブロック対象
        allowed, blocked = rs.filter_stop_loss_reentries(candidates, history, "2026-10-08")
        self.assertEqual(allowed, [])
        self.assertEqual([b["ticker"] for b in blocked], ["SELLER"])


if __name__ == "__main__":
    unittest.main()
