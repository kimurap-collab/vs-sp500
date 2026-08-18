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


class TestStopLoss(unittest.TestCase):
    """検証1-b: エントリー後に-8%到達 → 全株売却されること。"""

    def test_stop_loss_sells_all(self):
        lot = rs.new_lot("TST", "TST-1", "2026-01-05", filled_qty=300, fill_price=100.0)
        lot, trades = rs.simulate_lot_day(lot, 92.0, "2026-01-06")  # ちょうど-8%
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["kind"], "stop_loss")
        self.assertEqual(trades[0]["filled_qty"], 300)
        self.assertEqual(lot["shares"], 0)
        self.assertTrue(lot["closed"])
        self.assertEqual(lot["closed_reason"], "stop_loss")

    def test_price_above_stop_does_not_trigger(self):
        lot = rs.new_lot("TST", "TST-1", "2026-01-05", filled_qty=300, fill_price=100.0)
        lot, trades = rs.simulate_lot_day(lot, 92.5, "2026-01-06")  # -7.5%（未到達）
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
    """検証1-d: 10営業日目に+20%到達 → 利確されず、初期エントリーから56日後まで保持されること。
    検証1-e: その期間中に-8%到達 → 損切りが実行されること。"""

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

    def test_stop_loss_still_active_during_exception_hold(self):
        entry_date = "2026-01-05"
        lot = self._lot_after_pyramids(entry_date)
        avg_cost = lot["avg_cost"]

        day10 = "2026-01-19"
        lot, trades = rs.simulate_lot_day(lot, avg_cost * 1.20 * 1.001, day10)
        self.assertTrue(lot["exception_active"])

        # 例外ホールド中に初期エントリー価格の-8%まで下落 → 損切りが実行される
        stop_date = add_days(entry_date, 20)
        stop_price = lot["initial_entry_price"] * (1 + config.RSI_STOP_LOSS_PCT)
        lot, trades = rs.simulate_lot_day(lot, stop_price, stop_date)
        self.assertEqual([t["kind"] for t in trades], ["stop_loss"])
        self.assertTrue(lot["closed"])
        self.assertEqual(lot["shares"], 0)


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
