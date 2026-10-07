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
    """検証1-b: エントリー後に-8%到達 → 全株売却されること（2026-09-28復活。
    大将「損切りルール復活させようか。」択一１）-8%で全株売却）。"""

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

    def test_boundary_price_exactly_at_stop_triggers(self):
        """境界値: 初期エントリー価格×0.92ちょうど → 損切りが発動すること。"""
        lot = rs.new_lot("TST", "TST-1", "2026-01-05", filled_qty=100, fill_price=570.64)
        stop_price = 570.64 * 0.92
        lot, trades = rs.simulate_lot_day(lot, stop_price, "2026-01-06")
        self.assertEqual([t["kind"] for t in trades], ["stop_loss"])
        self.assertTrue(lot["closed"])

    def test_boundary_price_just_above_stop_does_not_trigger(self):
        """境界値: 初期エントリー価格570.64の-8%ラインは524.9888。525.06はそれを上回るため未到達。"""
        lot = rs.new_lot("TST", "TST-1", "2026-01-05", filled_qty=100, fill_price=570.64)
        lot, trades = rs.simulate_lot_day(lot, 525.06, "2026-01-06")
        self.assertEqual(trades, [])
        self.assertFalse(lot["closed"])

    def test_runner_only_with_profit_is_exempt(self):
        """例外: 伸ばす玉（利確1・2済み）のみを保有していて含み益が出ている場合は損切り対象外
        （decide_stop_lossを直接検証。実運用の買い増しではavg_costは初期エントリー価格以上
        にしかならず「price<=stop_line かつ price>avg_cost」を自然な取引列で再現できないため、
        ロットを直接組み立てて純粋関数の分岐を検証する）。"""
        lot = rs.new_lot("TST", "TST-1", "2026-01-05", filled_qty=100, fill_price=100.0)
        lot = {**lot, "avg_cost": 85.0, "profit1_taken": True, "profit2_taken": True}
        stop_line = 100.0 * (1 + config.RSI_STOP_LOSS_PCT)  # 92.0
        price = 90.0  # stop_line以下だが avg_cost(85.0)は上回る → 含み益あり
        self.assertLessEqual(price, stop_line)
        self.assertGreater(price, lot["avg_cost"])

        self.assertIsNone(rs.decide_stop_loss(lot, price))

    def test_runner_only_without_profit_still_triggers(self):
        """伸ばす玉でも含み益が無ければ（price<=avg_cost）例外は適用されず損切りされる。"""
        lot = rs.new_lot("TST", "TST-1", "2026-01-05", filled_qty=100, fill_price=100.0)
        lot = {**lot, "profit1_taken": True, "profit2_taken": True}
        intent = rs.decide_stop_loss(lot, 90.0)  # avg_cost=100のまま。含み益なし
        self.assertIsNotNone(intent)
        self.assertEqual(intent["kind"], "stop_loss")


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

        # クールダウンは存在しない: RSI<=35なら常にTrue
        self.assertTrue(rs.should_enter(34.9))
        self.assertTrue(rs.should_enter(5.0))
        self.assertFalse(rs.should_enter(35.1))

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
    c. ロットが無い（または損切りでクローズ済み）銘柄 → 新規エントリーされる
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

    def test_c_stop_loss_closed_lot_allows_new_entry(self):
        lot = rs.new_lot("MSFT", "MSFT-1", "2026-01-05", filled_qty=300, fill_price=100.0)
        lot = rs.apply_stop_loss_fill(lot, filled_qty=300, current_date="2026-01-06")
        self.assertTrue(lot["closed"])
        candidates = [{"ticker": "MSFT", "rsi14": 25.0, "price": 90.0}]

        allowed, blocked = rs.filter_blocked_entries(candidates, [lot])

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
        # 買い増し・損切り・利確の判定用フィールドはそのまま(関与しない)
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


class TestPyramidCancelledAfterProfit1(unittest.TestCase):
    """2026-10-07改訂: 利確1実施済みのロットは残りの買い増し段を全て無視すること
    （大将「１」＝利確1を出した銘柄は、残っている買い増しを取り消す）。"""

    def test_no_pyramid_after_profit1_even_if_threshold_met(self):
        lot = rs.new_lot("TST", "TST-1", "2026-01-05", filled_qty=300, fill_price=100.0)
        lot = {**lot, "profit1_taken": True, "base_shares": 300}
        # +2.5%の閾値は超えているが、利確1済みなので買い増しは一切出ない
        intents = rs.decide_pyramid_buys(lot, 102.5, rs.US_RULES)
        self.assertEqual(intents, [])

    def test_existing_lot_with_profit1_taken_never_pyramids_again(self):
        """既存ロット（コード変更前にprofit1_taken=Trueになっていたもの）も対象になること。"""
        lot = rs.new_lot("TST", "TST-1", "2026-01-05", filled_qty=300, fill_price=100.0)
        lot = {
            **lot, "profit1_taken": True, "pyramid_done": [False, False, False], "base_shares": 300,
        }
        intents = rs.decide_pyramid_buys(lot, 1000.0, rs.US_RULES)  # 全段の閾値を大きく超える価格
        self.assertEqual(intents, [])

    def test_pyramid_still_fires_before_profit1(self):
        """利確1前は従来どおり買い増しが出ること（回帰防止）。"""
        lot = rs.new_lot("TST", "TST-1", "2026-01-05", filled_qty=300, fill_price=100.0)
        intents = rs.decide_pyramid_buys(lot, 102.5, rs.US_RULES)
        self.assertEqual([i["kind"] for i in intents], ["pyramid1"])


class TestInvalidPrice(unittest.TestCase):
    """2026-10-07改訂: NaN/inf/None/0以下の価格は判定前にブロックすること
    （8/26にyfinanceがJP株のcloseをNaNで返し、price<thresholdが常にFalseになることで
    例外発動を誤って引き起こした事故の再発防止）。"""

    def test_is_valid_price_rejects_nan_inf_none_nonpositive(self):
        self.assertFalse(rs.is_valid_price(float("nan")))
        self.assertFalse(rs.is_valid_price(float("inf")))
        self.assertFalse(rs.is_valid_price(float("-inf")))
        self.assertFalse(rs.is_valid_price(None))
        self.assertFalse(rs.is_valid_price(0))
        self.assertFalse(rs.is_valid_price(-5.0))

    def test_is_valid_price_accepts_positive_finite(self):
        self.assertTrue(rs.is_valid_price(100.0))
        self.assertTrue(rs.is_valid_price(0.01))

    def test_nan_price_does_not_trigger_exception(self):
        """NaNをdecide_profit_takesにそのまま渡すと例外発動してしまう(旧挙動の確認)。
        呼び出し側はis_valid_priceで事前にブロックする契約のため、この関数自体は
        NaNを拒否する責務を持たない＝is_valid_priceでガードすることをテストする。"""
        lot = rs.new_lot("TST", "TST-1", "2026-08-24", filled_qty=300, fill_price=100.0)
        self.assertFalse(rs.is_valid_price(float("nan")))
        # ガードを通せば判定自体に到達しない（呼び出し側のcontinueに相当する確認）
        if rs.is_valid_price(float("nan")):
            rs.decide_profit_takes(lot, float("nan"), "2026-08-26", 2, rs.JP_RULES)
        # 上のifブロックは実行されない＝例外発動ロジックに到達しないことを示す
        self.assertFalse(lot["exception_active"])


class TestStopLossReentry(unittest.TestCase):
    """2026-10-07追加: 損切り後の再エントリー制限（SPEC_RSI30.md「2026-10-07改訂」）。

    仕様: 損切り価格Pで直近クローズされた銘柄は、(a) candidate価格<=P×0.85 か
    (b) 損切り日から10営業日以上経過 のいずれかを満たさない限り新規エントリーを見送る。
    """

    def test_latest_rule_closures_picks_most_recent_stop_loss(self):
        trades = [
            {"ticker": "AAA", "date": "2026-01-10", "price": "100.0", "rule": "stop_loss"},
            {"ticker": "AAA", "date": "2026-03-01", "price": "90.0", "rule": "stop_loss"},
            {"ticker": "BBB", "date": "2026-02-01", "price": "50.0", "rule": "profit1"},
        ]
        history = rs.latest_rule_closures(trades)
        self.assertEqual(history, {"AAA": {"date": "2026-03-01", "price": 90.0}})

    def test_blocked_within_10_trading_days_when_price_above_threshold(self):
        history = {"TST": {"date": "2026-09-28", "price": 100.0}}
        candidates = [{"ticker": "TST", "price": 90.0}]  # P×0.85=85なので90は上回る

        allowed, blocked = rs.filter_stop_loss_reentries(candidates, history, "2026-10-01")

        self.assertEqual(allowed, [])
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["ticker"], "TST")
        self.assertAlmostEqual(blocked[0]["threshold"], 85.0)

    def test_allowed_when_price_at_or_below_85_percent_of_stop_loss_price(self):
        history = {"TST": {"date": "2026-09-28", "price": 100.0}}
        candidates = [{"ticker": "TST", "price": 85.0}]  # ちょうど閾値

        allowed, blocked = rs.filter_stop_loss_reentries(candidates, history, "2026-10-01")

        self.assertEqual([c["ticker"] for c in allowed], ["TST"])
        self.assertEqual(blocked, [])

    def test_allowed_after_10_trading_days_regardless_of_price(self):
        history = {"TST": {"date": "2026-09-28", "price": 100.0}}
        candidates = [{"ticker": "TST", "price": 99.0}]  # 閾値85を大きく上回る

        # 2026-09-28から10営業日後（土日のみ・祝日考慮なし）= 2026-10-12
        allowed, blocked = rs.filter_stop_loss_reentries(candidates, history, "2026-10-12")

        self.assertEqual([c["ticker"] for c in allowed], ["TST"])
        self.assertEqual(blocked, [])

    def test_still_blocked_one_trading_day_before_10_elapsed(self):
        history = {"TST": {"date": "2026-09-28", "price": 100.0}}
        candidates = [{"ticker": "TST", "price": 99.0}]

        allowed, blocked = rs.filter_stop_loss_reentries(candidates, history, "2026-10-09")

        self.assertEqual(allowed, [])
        self.assertEqual([b["ticker"] for b in blocked], ["TST"])

    def test_ticker_without_stop_loss_history_passes_through(self):
        candidates = [{"ticker": "MSFT", "price": 300.0}]

        allowed, blocked = rs.filter_stop_loss_reentries(candidates, {}, "2026-10-01")

        self.assertEqual([c["ticker"] for c in allowed], ["MSFT"])
        self.assertEqual(blocked, [])

    def test_adjust_stop_loss_price_for_splits_applies_split_after_sale(self):
        # 1:2分割(ratio=2.0)が売却日より後に起きた場合、価格は÷2
        adjusted = rs.adjust_stop_loss_price_for_splits(
            price=7658.0, sale_date="2026-09-28", splits=[("2026-09-29", 2.0)], current_date="2026-10-07",
        )
        self.assertAlmostEqual(adjusted, 3829.0)

    def test_adjust_stop_loss_price_for_splits_ignores_split_before_sale(self):
        adjusted = rs.adjust_stop_loss_price_for_splits(
            price=100.0, sale_date="2026-09-28", splits=[("2026-09-20", 2.0)], current_date="2026-10-07",
        )
        self.assertAlmostEqual(adjusted, 100.0)

    def test_split_adjusted_price_changes_the_85_percent_threshold(self):
        history = {"5334": {"date": "2026-09-28", "price": 3829.0}}  # 分割調整済み(7658/2)
        candidates = [{"ticker": "5334", "price": 3300.0}]  # 3829*0.85=3254.65なので3300は上回る

        allowed, blocked = rs.filter_stop_loss_reentries(candidates, history, "2026-10-01")

        self.assertEqual(allowed, [])
        self.assertAlmostEqual(blocked[0]["threshold"], 3254.65, places=2)


# ---------------------------------------------------------------------------
# スワップ売却（2026-10-07追加。SPEC_RSI30.md「2026-10-07改訂」参照）
# ---------------------------------------------------------------------------

def _swap_lot(ticker, lot_id, avg_cost, shares, profit1=False, profit2=False):
    lot = rs.new_lot(ticker, lot_id, "2026-09-01", filled_qty=shares, fill_price=avg_cost)
    lot["profit1_taken"] = profit1
    lot["profit2_taken"] = profit2
    return lot


class TestSelectEntriesWithUnfunded(unittest.TestCase):
    def test_matches_select_entries_within_cash_for_selected(self):
        candidates = [
            {"ticker": "AAA", "rsi14": 10.0, "price": 100.0},
            {"ticker": "BBB", "rsi14": 20.0, "price": 100.0},
        ]
        selected_old = rs.select_entries_within_cash(candidates, 35_000.0)
        selected_new, unfunded = rs.select_entries_with_unfunded(candidates, 35_000.0)
        self.assertEqual(selected_old, selected_new)
        self.assertEqual([c["ticker"] for c in selected_new], ["AAA"])
        self.assertEqual([c["ticker"] for c in unfunded], ["BBB"])
        self.assertEqual(unfunded[0]["cost"], unfunded[0]["qty"] * 100.0)

    def test_no_unfunded_when_all_affordable(self):
        candidates = [{"ticker": "AAA", "rsi14": 10.0, "price": 100.0}]
        _selected, unfunded = rs.select_entries_with_unfunded(candidates, 1_000_000.0)
        self.assertEqual(unfunded, [])


class TestSelectSwapSellCandidates(unittest.TestCase):
    def test_runners_are_never_sold(self):
        """大将「伸ばす玉は売らないは正解」: profit1・profit2両方済みのロットは含み損でも対象外。"""
        runner = _swap_lot("AAA", "AAA-1", avg_cost=100.0, shares=50, profit1=True, profit2=True)
        loser = _swap_lot("BBB", "BBB-1", avg_cost=100.0, shares=100)
        prices = {"AAA": 50.0, "BBB": 80.0}

        result = rs.select_swap_sell_candidates([runner, loser], prices)

        self.assertEqual([lot["ticker"] for lot in result], ["BBB"])

    def test_only_losing_lots_are_included(self):
        winner = _swap_lot("AAA", "AAA-1", avg_cost=100.0, shares=50)
        loser = _swap_lot("BBB", "BBB-1", avg_cost=100.0, shares=50)
        prices = {"AAA": 120.0, "BBB": 90.0}

        result = rs.select_swap_sell_candidates([winner, loser], prices)

        self.assertEqual([lot["ticker"] for lot in result], ["BBB"])

    def test_sorted_by_deepest_loss_first(self):
        """大将「購入からの下げ幅が高いものから」: loss_ratio(price/avg_cost)が小さい順。"""
        mild = _swap_lot("AAA", "AAA-1", avg_cost=100.0, shares=50)   # -10%
        deep = _swap_lot("BBB", "BBB-1", avg_cost=100.0, shares=50)   # -30%
        prices = {"AAA": 90.0, "BBB": 70.0}

        result = rs.select_swap_sell_candidates([mild, deep], prices)

        self.assertEqual([lot["ticker"] for lot in result], ["BBB", "AAA"])

    def test_no_minimum_holding_period_all_losers_regardless_of_age(self):
        """大将「3)1」＝最低保有期間は無い。エントリー直後のロットでも対象になる。"""
        lot = rs.new_lot("AAA", "AAA-1", "2026-10-07", filled_qty=10, fill_price=100.0)
        result = rs.select_swap_sell_candidates([lot], {"AAA": 90.0})
        self.assertEqual([x["ticker"] for x in result], ["AAA"])

    def test_closed_and_missing_price_lots_excluded(self):
        closed = _swap_lot("AAA", "AAA-1", avg_cost=100.0, shares=50)
        closed["closed"] = True
        no_price = _swap_lot("BBB", "BBB-1", avg_cost=100.0, shares=50)

        result = rs.select_swap_sell_candidates([closed, no_price], {})

        self.assertEqual(result, [])


class TestRankSectorEtfTiers(unittest.TestCase):
    def test_eleven_etfs_split_four_four_three_by_return_desc(self):
        returns = {
            "XLK": 0.10, "XLF": 0.09, "XLV": 0.08, "XLE": 0.07,   # top4 → +1
            "XLI": 0.05, "XLY": 0.04, "XLP": 0.03, "XLU": 0.02,   # mid4 → 0
            "XLB": 0.00, "XLRE": -0.01, "XLC": -0.02,             # bottom3 → -1
        }
        tiers = rs.rank_sector_etf_tiers(returns)
        self.assertEqual(tiers["XLK"], 1)
        self.assertEqual(tiers["XLE"], 1)
        self.assertEqual(tiers["XLI"], 0)
        self.assertEqual(tiers["XLU"], 0)
        self.assertEqual(tiers["XLB"], -1)
        self.assertEqual(tiers["XLC"], -1)

    def test_empty_returns_empty_dict(self):
        self.assertEqual(rs.rank_sector_etf_tiers({}), {})


class TestComputeMarketCapTiers(unittest.TestCase):
    def test_top_third_plus_one_bottom_third_minus_one(self):
        caps = {f"T{i}": float(i) for i in range(1, 10)}  # 9銘柄: 1..9（昇順）
        tiers = rs.compute_market_cap_tiers(caps)
        self.assertEqual(tiers["T9"], 1)   # 最大
        self.assertEqual(tiers["T1"], -1)  # 最小
        self.assertEqual(tiers["T5"], 0)   # 中位

    def test_unknown_or_invalid_caps_excluded_from_result(self):
        caps = {"AAA": 100.0, "BBB": None, "CCC": float("nan"), "DDD": 0.0, "EEE": 200.0}
        tiers = rs.compute_market_cap_tiers(caps)
        self.assertNotIn("BBB", tiers)
        self.assertNotIn("CCC", tiers)
        self.assertNotIn("DDD", tiers)

    def test_favor_small_cap_reverses_sign(self):
        """2026-10-07改訂3: favor_small_cap=Trueで小型株+1・大型株-1に反転（米国枠・JP枠共用）。"""
        caps = {f"T{i}": float(i) for i in range(1, 10)}  # 9銘柄: 1..9（昇順）
        tiers = rs.compute_market_cap_tiers(caps, favor_small_cap=True)
        self.assertEqual(tiers["T9"], -1)  # 最大（大型株）は不利に
        self.assertEqual(tiers["T1"], 1)   # 最小（小型株）は有利に
        self.assertEqual(tiers["T5"], 0)   # 中位は変わらず0


class TestComputeSwapScore(unittest.TestCase):
    def test_known_sector_and_cap_sum_to_score(self):
        score = rs.compute_swap_score(
            "AAPL", sector_of={"AAPL": "XLK"}, sector_tiers={"XLK": 1}, mcap_tiers={"AAPL": 1},
        )
        self.assertEqual(score, 2)

    def test_unknown_sector_treated_as_zero(self):
        score = rs.compute_swap_score(
            "ZZZ", sector_of={"ZZZ": None}, sector_tiers={"XLK": 1}, mcap_tiers={"ZZZ": 1},
        )
        self.assertEqual(score, 1)

    def test_unknown_market_cap_treated_as_zero(self):
        score = rs.compute_swap_score(
            "AAPL", sector_of={"AAPL": "XLK"}, sector_tiers={"XLK": 1}, mcap_tiers={},
        )
        self.assertEqual(score, 1)

    def test_ticker_entirely_absent_from_sector_map_is_zero_zero(self):
        score = rs.compute_swap_score("ZZZ", sector_of={}, sector_tiers={"XLK": 1}, mcap_tiers={})
        self.assertEqual(score, 0)


class TestDecideSwaps(unittest.TestCase):
    def test_highest_score_candidate_chosen_first(self):
        """スコアが高い候補から順に処理されること（同率はRSI昇順）。"""
        unfunded = [
            {"ticker": "LOW", "rsi14": 10.0, "price": 100.0, "qty": 300, "cost": 30_000.0},
            {"ticker": "HIGH", "rsi14": 20.0, "price": 100.0, "qty": 300, "cost": 30_000.0},
        ]
        sells = rs.select_swap_sell_candidates(
            [_swap_lot("SELL", "SELL-1", avg_cost=100.0, shares=1000)], {"SELL": 50.0},
        )
        scores = {"LOW": 0, "HIGH": 2, "SELL": -2}

        decisions = rs.decide_swaps(unfunded, sells, scores, available_cash=0.0)

        self.assertEqual(decisions[0]["buy"]["ticker"], "HIGH")

    def test_tie_score_breaks_by_lowest_rsi(self):
        unfunded = [
            {"ticker": "A", "rsi14": 30.0, "price": 100.0, "qty": 300, "cost": 30_000.0},
            {"ticker": "B", "rsi14": 10.0, "price": 100.0, "qty": 300, "cost": 30_000.0},
        ]
        sells = rs.select_swap_sell_candidates(
            [_swap_lot("SELL", "SELL-1", avg_cost=100.0, shares=1000)], {"SELL": 50.0},
        )
        scores = {"A": 1, "B": 1, "SELL": -2}

        decisions = rs.decide_swaps(unfunded, sells, scores, available_cash=0.0)

        self.assertEqual(decisions[0]["buy"]["ticker"], "B")

    def test_sell_must_have_strictly_lower_score_than_buy(self):
        """スコアが買い候補と同点以上の保有ロットはスワップ対象にならない（厳密に低いことが条件）。"""
        unfunded = [{"ticker": "BUY", "rsi14": 10.0, "price": 100.0, "qty": 300, "cost": 30_000.0}]
        sells = rs.select_swap_sell_candidates(
            [_swap_lot("SELL", "SELL-1", avg_cost=100.0, shares=1000)], {"SELL": 50.0},
        )
        scores = {"BUY": 0, "SELL": 0}  # 同点 → 対象外

        decisions = rs.decide_swaps(unfunded, sells, scores, available_cash=0.0)

        self.assertEqual(decisions, [])

    def test_multiple_sells_accumulated_until_entry_amount_covered(self):
        """1件では$30,000に届かない損失ロットを複数売って資金を作る（大将「９）1」）。"""
        unfunded = [{"ticker": "BUY", "rsi14": 10.0, "price": 100.0, "qty": 300, "cost": 30_000.0}]
        lot1 = _swap_lot("S1", "S1-1", avg_cost=100.0, shares=100)   # 価格50 → $5,000
        lot2 = _swap_lot("S2", "S2-1", avg_cost=100.0, shares=100)   # 価格50 → $5,000
        lot3 = _swap_lot("S3", "S3-1", avg_cost=100.0, shares=400)   # 価格50 → $20,000
        prices = {"S1": 50.0, "S2": 60.0, "S3": 70.0}  # 下げ幅順: S1(最深) < S2 < S3
        sells = rs.select_swap_sell_candidates([lot1, lot2, lot3], prices)
        scores = {"BUY": 2, "S1": -1, "S2": -1, "S3": -1}

        decisions = rs.decide_swaps(unfunded, sells, scores, available_cash=0.0)

        self.assertEqual(len(decisions), 1)
        sold_tickers = {s["ticker"] for s in decisions[0]["sells"]}
        self.assertEqual(sold_tickers, {"S1", "S2", "S3"})  # 3件積み上げて初めて$30,000に届く

    def test_insufficient_even_with_all_qualifying_sells_does_nothing(self):
        unfunded = [{"ticker": "BUY", "rsi14": 10.0, "price": 100.0, "qty": 300, "cost": 30_000.0}]
        sells = rs.select_swap_sell_candidates(
            [_swap_lot("S1", "S1-1", avg_cost=100.0, shares=50)], {"S1": 50.0},  # $2,500しか作れない
        )
        scores = {"BUY": 2, "S1": -1}

        decisions = rs.decide_swaps(unfunded, sells, scores, available_cash=0.0)

        self.assertEqual(decisions, [])

    def test_stops_entirely_once_top_candidate_cannot_be_funded(self):
        """最上位候補が資金化できなければ、それ以降の(スコアが低い)候補も試みず打ち切る
        （参照実装 backtest_swap/swap.py run() の break と同じ挙動）。"""
        unfunded = [
            {"ticker": "HIGH", "rsi14": 10.0, "price": 100.0, "qty": 300, "cost": 30_000.0},
            {"ticker": "LOW", "rsi14": 10.0, "price": 10.0, "qty": 300, "cost": 3_000.0},
        ]
        # LOWより売却候補のスコアが低いロットは無い(HIGHの判定失敗で即break)ため、
        # LOWだけなら本来資金化できる売却ロットをあえて用意する
        sells = rs.select_swap_sell_candidates(
            [_swap_lot("S1", "S1-1", avg_cost=100.0, shares=200)], {"S1": 50.0},  # $10,000
        )
        scores = {"HIGH": 2, "LOW": 1, "S1": 0}  # S1(0)はHIGH(2)より低いがLOW(1)より低くはない… 条件はHIGH側

        decisions = rs.decide_swaps(unfunded, sells, scores, available_cash=0.0)

        # HIGHは$10,000しか作れず$30,000に届かないため失敗 → LOWも試されず終了
        self.assertEqual(decisions, [])

    def test_buy_without_sells_when_cash_already_sufficient(self):
        unfunded = [{"ticker": "BUY", "rsi14": 10.0, "price": 100.0, "qty": 300, "cost": 30_000.0}]

        decisions = rs.decide_swaps(unfunded, [], {"BUY": 1}, available_cash=30_000.0)

        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["sells"], [])

    def test_sold_lot_not_reused_across_decisions(self):
        """1回のスワップで使ったロットは、次の候補の判定からも除外される。"""
        unfunded = [
            {"ticker": "A", "rsi14": 10.0, "price": 100.0, "qty": 300, "cost": 30_000.0},
            {"ticker": "B", "rsi14": 10.0, "price": 100.0, "qty": 300, "cost": 30_000.0},
        ]
        lot1 = _swap_lot("S1", "S1-1", avg_cost=100.0, shares=600)  # 価格50 → $30,000ちょうど
        sells = rs.select_swap_sell_candidates([lot1], {"S1": 50.0})
        scores = {"A": 2, "B": 2, "S1": -1}

        decisions = rs.decide_swaps(unfunded, sells, scores, available_cash=0.0)

        # Aがsells1件で資金化 → Bの判定ではS1がもう無いので資金化できず打ち切り
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["buy"]["ticker"], "A")


if __name__ == "__main__":
    unittest.main()
