"""rsi_strategy.py の単体テスト（SPEC_RSI30.md 検証1のa〜f・検証2）。

実データ・broker接続を一切使わず、作った価格列だけで判定する。
pytestが未インストールの環境のため標準ライブラリのunittestで書く。
実行: python3 -m unittest test_rsi_strategy.py -v
"""
from __future__ import annotations

import datetime as dt
import unittest

import config
import rsi_strategy as rs


def add_days(date_str: str, days: int) -> str:
    return (dt.date.fromisoformat(date_str) + dt.timedelta(days=days)).isoformat()


class TestPyramid(unittest.TestCase):
    """検証1-a: エントリー後に+2.5%/+5%/+7.5%と上昇 → 買い増しが3回入り、
    総額$60,000・平均取得単価が正しいこと。"""

    def test_three_pyramid_stages_fill_correctly(self):
        lot = rs.new_lot("TST", "TST-1", "2026-01-05", filled_qty=300, fill_price=100.0)
        self.assertEqual(lot["total_invested_usd"], 30000.0)

        lot, trades1 = rs.simulate_lot_day(lot, 102.5, "2026-01-06")  # +2.5%
        self.assertEqual([t["kind"] for t in trades1], ["pyramid1"])
        self.assertEqual(trades1[0]["filled_qty"], 146)  # floor(15000/102.5)
        self.assertTrue(lot["pyramid_done"][0])

        lot, trades2 = rs.simulate_lot_day(lot, 105.0, "2026-01-07")  # +5.0%
        self.assertEqual([t["kind"] for t in trades2], ["pyramid2"])
        self.assertEqual(trades2[0]["filled_qty"], 71)  # floor(7500/105)
        self.assertTrue(lot["pyramid_done"][1])

        lot, trades3 = rs.simulate_lot_day(lot, 107.5, "2026-01-08")  # +7.5%
        self.assertEqual([t["kind"] for t in trades3], ["pyramid3"])
        self.assertEqual(trades3[0]["filled_qty"], 69)  # floor(7500/107.5)
        self.assertTrue(lot["pyramid_done"][2])

        expected_shares = 300 + 146 + 71 + 69
        expected_invested = 30000.0 + 146 * 102.5 + 71 * 105.0 + 69 * 107.5
        self.assertEqual(lot["shares"], expected_shares)
        self.assertAlmostEqual(lot["total_invested_usd"], expected_invested, places=6)
        self.assertAlmostEqual(lot["avg_cost"], expected_invested / expected_shares, places=6)
        # 4段合計は$60,000上限に収まる(整数株の切り捨てにより厳密には下回る)
        lot_max = config.RSI_ENTRY_AMOUNT_USD + sum(config.RSI_PYRAMID_AMOUNTS_USD)
        self.assertLessEqual(lot["total_invested_usd"], lot_max)
        self.assertGreater(lot["total_invested_usd"], lot_max * 0.95)

    def test_no_double_fill_same_stage(self):
        """一度実施した段は閾値を超え続けても再度買い増ししない。"""
        lot = rs.new_lot("TST", "TST-1", "2026-01-05", filled_qty=300, fill_price=100.0)
        lot, _ = rs.simulate_lot_day(lot, 102.5, "2026-01-06")
        lot, trades = rs.simulate_lot_day(lot, 103.0, "2026-01-07")  # まだ+5%未満
        self.assertEqual(trades, [])


class TestNoStopLoss(unittest.TestCase):
    """2026-08-19改訂（損切りルール削除。大将「損切りのルールは削除しようか」）:
    株価が初期エントリー価格を大きく下回っても売却が発生しないこと。"""

    def test_large_drawdown_does_not_trigger_a_sell(self):
        lot = rs.new_lot("TST", "TST-1", "2026-01-05", filled_qty=300, fill_price=100.0)
        lot, trades = rs.simulate_lot_day(lot, 70.0, "2026-01-06")  # -30%
        self.assertEqual(trades, [])
        self.assertEqual(lot["shares"], 300)
        self.assertFalse(lot["closed"])

    def test_drawdown_during_exception_hold_does_not_trigger_a_sell(self):
        """例外（8週ホールド）中に大きく下落しても、旧損切りルールは既に無いため何も起きない。"""
        entry_date = "2026-01-05"
        lot = rs.new_lot("TST", "TST-1", entry_date, filled_qty=300, fill_price=100.0)
        lot, _ = rs.simulate_lot_day(lot, 102.5, add_days(entry_date, 1))
        lot, _ = rs.simulate_lot_day(lot, 105.0, add_days(entry_date, 2))
        lot, _ = rs.simulate_lot_day(lot, 107.5, add_days(entry_date, 3))
        avg_cost = lot["avg_cost"]

        day10 = "2026-01-19"
        lot, trades = rs.simulate_lot_day(lot, avg_cost * 1.20 * 1.001, day10)
        self.assertTrue(lot["exception_active"])

        drawdown_date = add_days(entry_date, 20)
        lot, trades = rs.simulate_lot_day(lot, lot["initial_entry_price"] * 0.70, drawdown_date)  # -30%
        self.assertEqual(trades, [])
        self.assertFalse(lot["closed"])


class TestProfitTaking(unittest.TestCase):
    """検証1-c: 買い増し後に平均取得単価+20% → 50%売却、+25% → さらに25%売却、25%が残ること。"""

    def _lot_after_pyramids(self):
        lot = rs.new_lot("TST", "TST-1", "2026-01-05", filled_qty=300, fill_price=100.0)
        lot, _ = rs.simulate_lot_day(lot, 102.5, "2026-01-06")
        lot, _ = rs.simulate_lot_day(lot, 105.0, "2026-01-07")
        lot, _ = rs.simulate_lot_day(lot, 107.5, "2026-01-08")
        return lot

    def test_profit1_then_profit2_leaves_25pct_runner(self):
        lot = self._lot_after_pyramids()
        base_shares = lot["shares"]  # 978
        avg_cost = lot["avg_cost"]

        # trading_days_elapsedを15超にして例外に掛からないようにする（2026-03-02は約42営業日後）
        profit1_price = avg_cost * 1.20 * 1.001
        lot, trades = rs.simulate_lot_day(lot, profit1_price, "2026-03-02")
        self.assertEqual([t["kind"] for t in trades], ["profit1"])
        expected_qty1 = int(base_shares * 0.5)
        self.assertEqual(trades[0]["filled_qty"], expected_qty1)
        self.assertTrue(lot["profit1_taken"])
        self.assertEqual(lot["base_shares"], base_shares)
        self.assertEqual(lot["shares"], base_shares - expected_qty1)

        profit2_price = avg_cost * 1.25 * 1.001
        lot, trades = rs.simulate_lot_day(lot, profit2_price, "2026-03-03")
        self.assertEqual([t["kind"] for t in trades], ["profit2"])
        expected_qty2 = int(base_shares * 0.25)
        self.assertEqual(trades[0]["filled_qty"], expected_qty2)
        self.assertTrue(lot["profit2_taken"])

        remaining = lot["shares"]
        self.assertEqual(remaining, base_shares - expected_qty1 - expected_qty2)
        # 25%が残ること（端数の切り捨て分で1株程度前後しうる）
        self.assertAlmostEqual(remaining, base_shares * 0.25, delta=1)

        # 伸ばす玉はその後どれだけ価格が上がっても追加売却されない
        lot, trades = rs.simulate_lot_day(lot, avg_cost * 2.0, "2026-03-04")
        self.assertEqual(trades, [])
        self.assertEqual(lot["shares"], remaining)


class TestException15Day(unittest.TestCase):
    """検証1-d: 10営業日目に+20%到達 → 利確されず、初期エントリーから56日後まで保持されること。"""

    def _lot_after_pyramids(self, entry_date="2026-01-05"):
        lot = rs.new_lot("TST", "TST-1", entry_date, filled_qty=300, fill_price=100.0)
        lot, _ = rs.simulate_lot_day(lot, 102.5, add_days(entry_date, 1))
        lot, _ = rs.simulate_lot_day(lot, 105.0, add_days(entry_date, 2))
        lot, _ = rs.simulate_lot_day(lot, 107.5, add_days(entry_date, 3))
        return lot

    def test_exception_triggers_and_holds_until_deadline(self):
        entry_date = "2026-01-05"  # 月曜
        lot = self._lot_after_pyramids(entry_date)
        avg_cost = lot["avg_cost"]
        shares_before = lot["shares"]

        day10 = "2026-01-19"  # entry_dateから10営業日目
        self.assertEqual(rs.business_days_since(entry_date, day10), 10)

        price = avg_cost * 1.20 * 1.001
        lot, trades = rs.simulate_lot_day(lot, price, day10)
        self.assertEqual([t["kind"] for t in trades], ["exception_trigger"])
        self.assertTrue(lot["exception_active"])
        expected_deadline = (dt.date.fromisoformat(entry_date) + dt.timedelta(days=56)).isoformat()
        self.assertEqual(lot["exception_deadline_date"], expected_deadline)
        # 売っていない
        self.assertEqual(lot["shares"], shares_before)
        self.assertFalse(lot["profit1_taken"])

        # 締切前・価格がさらに上がっても利確されない
        mid_date = add_days(entry_date, 30)
        lot, trades = rs.simulate_lot_day(lot, avg_cost * 1.5, mid_date)
        self.assertEqual(trades, [])
        self.assertEqual(lot["shares"], shares_before)

        # 締切到達後は通常の利確ルールに戻る（+20%と+25%の間の価格なので利確1のみ発火）
        lot, trades = rs.simulate_lot_day(lot, avg_cost * 1.22, expected_deadline)
        self.assertEqual([t["kind"] for t in trades], ["profit1"])
        self.assertTrue(lot["profit1_taken"])

class TestReentry(unittest.TestCase):
    """検証1-f: 伸ばす玉のみ保有中に再びRSI≤30 → 別ロットとして新規建てされること。"""

    def test_runner_only_lot_allows_independent_new_lot(self):
        lot = rs.new_lot("TST", "TST-1", "2026-01-05", filled_qty=300, fill_price=100.0)
        lot, _ = rs.simulate_lot_day(lot, 102.5, "2026-01-06")
        lot, _ = rs.simulate_lot_day(lot, 105.0, "2026-01-07")
        lot, _ = rs.simulate_lot_day(lot, 107.5, "2026-01-08")
        avg_cost = lot["avg_cost"]
        lot, _ = rs.simulate_lot_day(lot, avg_cost * 1.20 * 1.001, "2026-03-02")
        lot, _ = rs.simulate_lot_day(lot, avg_cost * 1.25 * 1.001, "2026-03-03")
        self.assertTrue(lot["profit1_taken"] and lot["profit2_taken"])
        runner_shares = lot["shares"]
        self.assertGreater(runner_shares, 0)

        # クールダウンは存在しない: RSI<=32なら常にTrue
        self.assertTrue(rs.should_enter(31.9))
        self.assertTrue(rs.should_enter(5.0))
        self.assertFalse(rs.should_enter(32.1))

        new_lot = rs.new_lot("TST", "TST-2", "2026-03-10", filled_qty=400, fill_price=80.0)
        # 新ロットは既存ロットと完全に独立
        self.assertNotEqual(new_lot["lot_id"], lot["lot_id"])
        self.assertEqual(new_lot["shares"], 400)
        self.assertEqual(new_lot["initial_entry_price"], 80.0)
        # 既存ロット(伸ばす玉)は一切変更されない
        self.assertEqual(lot["shares"], runner_shares)
        self.assertTrue(lot["profit1_taken"] and lot["profit2_taken"])


class TestFilterBlockedEntries(unittest.TestCase):
    """改修1検証（2026-08-19・大将「１だな」）: 同じ銘柄は保有中1ロットまで。

    a. 未クローズかつ利確前のロットがある銘柄 → 新規エントリーされない
    b. 未クローズだが利確1実施済みのロットがある銘柄 → 新規エントリーされる
    c. ロットが無い銘柄 → 新規エントリーされる
    """

    def test_a_unclosed_lot_before_any_profit_blocks_new_entry(self):
        lot = rs.new_lot("DVA", "DVA-1", "2026-08-18", filled_qty=169, fill_price=177.64)
        candidates = [{"ticker": "DVA", "rsi14": 25.0, "price": 170.0}]

        allowed, blocked = rs.filter_blocked_entries(candidates, [lot])

        self.assertEqual(allowed, [])
        self.assertEqual(blocked, ["DVA"])

    def test_b_unclosed_lot_after_profit1_allows_new_entry(self):
        lot = rs.new_lot("AAPL", "AAPL-1", "2026-01-05", filled_qty=300, fill_price=100.0)
        lot = rs.apply_profit1_fill(lot, filled_qty=150, base_shares=300)  # 伸ばす玉の状態
        self.assertFalse(lot["closed"])
        self.assertTrue(lot["profit1_taken"])
        candidates = [{"ticker": "AAPL", "rsi14": 25.0, "price": 90.0}]

        allowed, blocked = rs.filter_blocked_entries(candidates, [lot])

        self.assertEqual([c["ticker"] for c in allowed], ["AAPL"])
        self.assertEqual(blocked, [])

    def test_c_no_lot_allows_new_entry(self):
        candidates = [{"ticker": "MSFT", "rsi14": 25.0, "price": 300.0}]

        allowed, blocked = rs.filter_blocked_entries(candidates, [])

        self.assertEqual([c["ticker"] for c in allowed], ["MSFT"])
        self.assertEqual(blocked, [])

    def test_unrelated_tickers_pass_through_untouched(self):
        blocked_lot = rs.new_lot("DVA", "DVA-1", "2026-08-18", filled_qty=169, fill_price=177.64)
        candidates = [
            {"ticker": "DVA", "rsi14": 25.0, "price": 170.0},
            {"ticker": "MSFT", "rsi14": 20.0, "price": 300.0},
        ]

        allowed, blocked = rs.filter_blocked_entries(candidates, [blocked_lot])

        self.assertEqual([c["ticker"] for c in allowed], ["MSFT"])
        self.assertEqual(blocked, ["DVA"])
        # 買い増し・利確の判定用フィールドはそのまま(関与しない)
        self.assertFalse(blocked_lot["profit1_taken"])


class TestCashPriority(unittest.TestCase):
    """検証2: 現金不足時にRSIの低い順で選ばれること（信号5件・現金2件分で検証）。"""

    def test_lowest_rsi_selected_first_when_cash_limited(self):
        candidates = [
            {"ticker": "A", "rsi14": 28.0, "price": 100.0},
            {"ticker": "B", "rsi14": 15.0, "price": 100.0},
            {"ticker": "C", "rsi14": 25.0, "price": 100.0},
            {"ticker": "D", "rsi14": 10.0, "price": 100.0},
            {"ticker": "E", "rsi14": 29.0, "price": 100.0},
        ]
        # 1件$30,000 x 2件分だけ現金がある(5件中2件しか買えない)
        available_cash = config.RSI_ENTRY_AMOUNT_USD * 2
        selected = rs.select_entries_within_cash(candidates, available_cash)
        self.assertEqual([c["ticker"] for c in selected], ["D", "B"])  # RSI 10, 15の順

    def test_skips_unaffordable_and_continues_to_next(self):
        candidates = [
            {"ticker": "CHEAP", "rsi14": 20.0, "price": 100.0},   # qty300, $30,000
            {"ticker": "PRICEY", "rsi14": 5.0, "price": 100000.0},  # 1株も買えない
        ]
        selected = rs.select_entries_within_cash(candidates, available_cash=50000.0)
        self.assertEqual([c["ticker"] for c in selected], ["CHEAP"])


if __name__ == "__main__":
    unittest.main()
