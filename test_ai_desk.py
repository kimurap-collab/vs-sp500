"""AI裁量枠のガードレールと台帳の単体テスト。

この枠は売買ルールを持たない（AIが毎回決める）ので、テストするのは
「AIが何を言おうとコードが止めるもの」＝ガードレールだけである。
実データ・broker接続・Anthropic APIは一切使わない。

実行: python3 -m unittest test_ai_desk.py -v
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

import ai_ledger
import config


def make_state(cash=100_000.0, positions=None, pending=None, placed=None):
    return {
        "start_date": "2026-08-28",
        "cash_usd": cash,
        "positions": positions or {},
        "bench_units_ai": 141.0,
        "last_processed_date": None,
        "pending_orders": pending or [],
        "orders_placed_today": placed or {"date": None, "count": 0},
    }


def position(shares, avg_cost, name="TEST"):
    return {
        "shares": shares, "total_cost_usd": shares * avg_cost, "avg_cost": avg_cost,
        "name": name, "first_entry_date": "2026-08-20",
    }


REASON = "決算で売上ガイダンスが上振れした一方、株価は決算前の水準に戻っており織り込みが浅いと判断した"


class TestOtherBooksAreUntouchable(unittest.TestCase):
    """他の枠の玉・口座の玉を売ろうとしても弾かれること。"""

    def test_sell_of_ticker_not_in_own_ledger_is_rejected(self):
        # RSI枠が保有しているTJX、本体が保有しているVOOをAI裁量枠が売ろうとする
        state = make_state(positions={"AAPL": position(50, 200.0)})
        orders = [
            {"action": "SELL", "ticker": "TJX", "sell_fraction": 1.0, "rule": "exit", "reason": REASON},
            {"action": "SELL", "ticker": "VOO", "sell_fraction": 1.0, "rule": "exit", "reason": REASON},
        ]
        accepted, rejected = ai_ledger.validate_orders(
            orders, state, {"TJX": 120.0, "VOO": 708.75, "AAPL": 210.0}, {"TJX", "AAPL"}, "2026-08-28",
        )
        self.assertEqual(accepted, [])
        self.assertEqual(len(rejected), 2)
        for r in rejected:
            self.assertIn("台帳に保有が無い", r["reject_reason"])

    def test_sell_more_shares_than_owned_is_capped_not_shorted(self):
        state = make_state(positions={"AAPL": position(10, 200.0)})
        orders = [{"action": "SELL", "ticker": "AAPL", "sell_fraction": 1.0, "rule": "exit", "reason": REASON}]
        accepted, rejected = ai_ledger.validate_orders(
            orders, state, {"AAPL": 210.0}, {"AAPL"}, "2026-08-28",
        )
        self.assertEqual(rejected, [])
        self.assertEqual(accepted[0]["qty"], 10)  # 保有株数ちょうど。超えない

    def test_two_sells_of_same_ticker_cannot_exceed_holding(self):
        """同じ銘柄を2回売っても、合計が保有株数を超えないこと（空売りの禁止）。"""
        state = make_state(positions={"AAPL": position(10, 200.0)})
        orders = [
            {"action": "SELL", "ticker": "AAPL", "sell_fraction": 1.0, "rule": "exit", "reason": REASON},
            {"action": "SELL", "ticker": "AAPL", "sell_fraction": 1.0, "rule": "exit", "reason": REASON},
        ]
        accepted, rejected = ai_ledger.validate_orders(
            orders, state, {"AAPL": 210.0}, {"AAPL"}, "2026-08-28",
        )
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["qty"], 10)
        self.assertEqual(len(rejected), 1)

    def test_pending_sell_reserves_shares(self):
        """未決のSELLで出している分は二重に売れないこと。"""
        state = make_state(
            positions={"AAPL": position(10, 200.0)},
            pending=[{"order_id": "1", "ticker": "AAPL", "side": "SELL", "qty": 10, "applied_qty": 0}],
        )
        orders = [{"action": "SELL", "ticker": "AAPL", "sell_fraction": 1.0, "rule": "exit", "reason": REASON}]
        accepted, rejected = ai_ledger.validate_orders(
            orders, state, {"AAPL": 210.0}, {"AAPL"}, "2026-08-28",
        )
        self.assertEqual(accepted, [])
        self.assertIn("保有が無い", rejected[0]["reject_reason"])


class TestCashGuard(unittest.TestCase):
    """現金を超える発注が弾かれること（レバレッジの禁止）。"""

    def test_single_order_over_cash_is_rejected(self):
        state = make_state(cash=10_000.0)
        orders = [{"action": "BUY", "ticker": "AAPL", "amount_usd": 50_000.0, "rule": "new_position", "reason": REASON}]
        accepted, rejected = ai_ledger.validate_orders(
            orders, state, {"AAPL": 200.0}, {"AAPL"}, "2026-08-28",
        )
        self.assertEqual(accepted, [])
        self.assertIn("現金不足", rejected[0]["reject_reason"])

    def test_orders_are_cumulative_against_cash(self):
        """1件ずつは現金内でも、合計で超える分は弾かれること。"""
        state = make_state(cash=10_000.0)
        orders = [
            {"action": "BUY", "ticker": "AAA", "amount_usd": 6_000.0, "rule": "new_position", "reason": REASON},
            {"action": "BUY", "ticker": "BBB", "amount_usd": 6_000.0, "rule": "new_position", "reason": REASON},
        ]
        accepted, rejected = ai_ledger.validate_orders(
            orders, state, {"AAA": 100.0, "BBB": 100.0}, {"AAA", "BBB"}, "2026-08-28",
        )
        self.assertEqual([a["ticker"] for a in accepted], ["AAA"])
        self.assertIn("現金不足", rejected[0]["reject_reason"])

    def test_pending_buy_reserves_cash(self):
        state = make_state(
            cash=10_000.0,
            pending=[{"order_id": "1", "ticker": "AAA", "side": "BUY", "qty": 90,
                      "applied_qty": 0, "est_price": 100.0}],
        )
        self.assertAlmostEqual(ai_ledger.compute_available_cash(state, {}), 1_000.0)
        orders = [{"action": "BUY", "ticker": "BBB", "amount_usd": 5_000.0, "rule": "new_position", "reason": REASON}]
        accepted, rejected = ai_ledger.validate_orders(
            orders, state, {"BBB": 100.0}, {"BBB"}, "2026-08-28",
        )
        self.assertEqual(accepted, [])
        self.assertIn("現金不足", rejected[0]["reject_reason"])

    def test_amount_smaller_than_one_share_is_rejected(self):
        state = make_state(cash=100_000.0)
        orders = [{"action": "BUY", "ticker": "BRK-A", "amount_usd": 100.0, "rule": "new_position", "reason": REASON}]
        accepted, rejected = ai_ledger.validate_orders(
            orders, state, {"BRK-A": 800_000.0}, {"BRK-A"}, "2026-08-28",
        )
        self.assertEqual(accepted, [])
        self.assertIn("1株も買えない", rejected[0]["reject_reason"])


class TestDailyOrderCap(unittest.TestCase):
    """1日の発注上限が効くこと。"""

    def test_cap_limits_orders_in_one_batch(self):
        state = make_state(cash=1_000_000.0)
        orders = [
            {"action": "BUY", "ticker": f"T{i}", "amount_usd": 1_000.0, "rule": "new_position", "reason": REASON}
            for i in range(config.AI_MAX_ORDERS_PER_DAY + 3)
        ]
        prices = {f"T{i}": 100.0 for i in range(config.AI_MAX_ORDERS_PER_DAY + 3)}
        accepted, rejected = ai_ledger.validate_orders(
            orders, state, prices, set(prices), "2026-08-28",
        )
        self.assertEqual(len(accepted), config.AI_MAX_ORDERS_PER_DAY)
        self.assertEqual(len(rejected), 3)
        self.assertIn("発注上限", rejected[0]["reject_reason"])

    def test_orders_already_placed_today_count_against_the_cap(self):
        state = make_state(cash=1_000_000.0, placed={"date": "2026-08-28", "count": config.AI_MAX_ORDERS_PER_DAY - 1})
        orders = [
            {"action": "BUY", "ticker": "AAA", "amount_usd": 1_000.0, "rule": "new_position", "reason": REASON},
            {"action": "BUY", "ticker": "BBB", "amount_usd": 1_000.0, "rule": "new_position", "reason": REASON},
        ]
        accepted, rejected = ai_ledger.validate_orders(
            orders, state, {"AAA": 100.0, "BBB": 100.0}, {"AAA", "BBB"}, "2026-08-28",
        )
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(rejected), 1)

    def test_counter_from_another_day_does_not_carry_over(self):
        state = make_state(cash=1_000_000.0, placed={"date": "2026-08-27", "count": 5})
        self.assertEqual(ai_ledger.orders_placed_today(state, "2026-08-28"), 0)


class TestTradableUniverseGuard(unittest.TestCase):
    """対象外の銘柄（ETF・レバレッジ商品・店頭銘柄）が買えないこと。"""

    def test_buy_of_ticker_outside_tradable_set_is_rejected(self):
        state = make_state()
        orders = [
            {"action": "BUY", "ticker": "TQQQ", "amount_usd": 10_000.0, "rule": "new_position", "reason": REASON},
        ]
        accepted, rejected = ai_ledger.validate_orders(
            orders, state, {"TQQQ": 100.0}, {"AAPL"}, "2026-08-28",
        )
        self.assertEqual(accepted, [])
        self.assertIn("普通株ではない", rejected[0]["reject_reason"])

    def test_order_without_price_is_rejected(self):
        state = make_state()
        orders = [{"action": "BUY", "ticker": "ZZZZ", "amount_usd": 1_000.0, "rule": "new_position", "reason": REASON}]
        accepted, rejected = ai_ledger.validate_orders(orders, state, {}, {"ZZZZ"}, "2026-08-28")
        self.assertEqual(accepted, [])
        self.assertIn("現在値が取得できない", rejected[0]["reject_reason"])


class TestReasonIsMandatory(unittest.TestCase):
    """理由の無い注文・中身のない一言だけの注文が通らないこと。"""

    def test_empty_reason_is_rejected(self):
        state = make_state()
        orders = [{"action": "BUY", "ticker": "AAPL", "amount_usd": 1_000.0, "rule": "new_position", "reason": ""}]
        accepted, rejected = ai_ledger.validate_orders(
            orders, state, {"AAPL": 100.0}, {"AAPL"}, "2026-08-28",
        )
        self.assertEqual(accepted, [])
        self.assertIn("理由が短すぎる", rejected[0]["reject_reason"])

    def test_one_word_reason_is_rejected(self):
        state = make_state()
        orders = [{"action": "BUY", "ticker": "AAPL", "amount_usd": 1_000.0, "rule": "new_position", "reason": "割安"}]
        accepted, rejected = ai_ledger.validate_orders(
            orders, state, {"AAPL": 100.0}, {"AAPL"}, "2026-08-28",
        )
        self.assertEqual(accepted, [])
        self.assertIn("理由が短すぎる", rejected[0]["reject_reason"])


class TestUnknownAction(unittest.TestCase):
    def test_short_and_other_actions_are_rejected(self):
        state = make_state()
        orders = [
            {"action": "SHORT", "ticker": "AAPL", "amount_usd": 1_000.0, "rule": "new_position", "reason": REASON},
            {"action": "", "ticker": "AAPL", "amount_usd": 1_000.0, "rule": "new_position", "reason": REASON},
        ]
        accepted, rejected = ai_ledger.validate_orders(
            orders, state, {"AAPL": 100.0}, {"AAPL"}, "2026-08-28",
        )
        self.assertEqual(accepted, [])
        self.assertEqual(len(rejected), 2)


class TestNoRealAccountPath(unittest.TestCase):
    """実口座への発注経路が存在しないこと。"""

    SOURCE_FILES = [
        "broker.py", "ai_daily.py", "ai_agents.py", "ai_ledger.py",
        "rsi_daily.py", "jp_rsi_daily.py", "portfolio.py", "daily_run.py",
    ]

    def _sources(self):
        base = Path(config.BASE_DIR)
        for name in self.SOURCE_FILES:
            path = base / name
            if path.exists():
                yield name, path.read_text(encoding="utf-8")

    def test_trd_env_real_is_never_used(self):
        for name, text in self._sources():
            self.assertNotIn("TrdEnv.REAL", text, f"{name} に実口座への発注経路がある")

    def test_unlock_trade_is_never_called(self):
        for name, text in self._sources():
            self.assertIsNone(
                re.search(r"\.unlock_trade\s*\(", text),
                f"{name} が unlock_trade を呼んでいる（取引パスワードは大将が保持する）",
            )

    def test_every_order_path_pins_simulate_and_the_account_id(self):
        """発注・照会は必ず TrdEnv.SIMULATE と config.MOOMOO_ACC_ID を指定していること。"""
        text = (Path(config.BASE_DIR) / "broker.py").read_text(encoding="utf-8")
        # 実際の呼び出し（ctx.xxx(...)）だけを見る。docstring中の言及は対象外
        calls = ("ctx.place_order(", "ctx.order_list_query(", "ctx.position_list_query(",
                 "ctx.accinfo_query(", "ctx.history_order_list_query(", "ctx.modify_order(")
        found = 0
        for call in calls:
            start = 0
            while True:
                idx = text.find(call, start)
                if idx == -1:
                    break
                found += 1
                chunk = text[idx: idx + 420]
                self.assertIn("TrdEnv.SIMULATE", chunk, f"{call} がSIMULATEを指定していない")
                self.assertIn("config.MOOMOO_ACC_ID", chunk, f"{call} がacc_idを固定していない")
                start = idx + 1
        self.assertGreaterEqual(found, len(calls), "発注・照会の呼び出しを検出できていない（テストが空回り）")

    def test_ai_desk_places_orders_only_through_broker(self):
        """AI裁量枠がmoomooのplace_orderを直接呼ばず、必ずbroker.py経由であること。"""
        for name in ("ai_daily.py", "ai_agents.py", "ai_ledger.py"):
            text = (Path(config.BASE_DIR) / name).read_text(encoding="utf-8")
            self.assertNotIn("place_order(", text, f"{name} がmoomooのplace_orderを直接呼んでいる")


class TestPositionMath(unittest.TestCase):
    """玉の更新（平均取得単価・実現損益・決着判定）。"""

    def test_buy_then_add_updates_avg_cost(self):
        state = ai_ledger.apply_buy_fill(make_state(), "AAA", 10, 100.0, "2026-08-28", "A社")
        state = ai_ledger.apply_buy_fill(state, "AAA", 10, 120.0, "2026-08-29", "A社")
        pos = state["positions"]["AAA"]
        self.assertEqual(pos["shares"], 20)
        self.assertAlmostEqual(pos["avg_cost"], 110.0)
        self.assertEqual(pos["first_entry_date"], "2026-08-28")

    def test_partial_sell_keeps_position_and_returns_no_review(self):
        state = ai_ledger.apply_buy_fill(make_state(), "AAA", 10, 100.0, "2026-08-28")
        state, closed = ai_ledger.apply_sell_fill(state, "AAA", 4, 130.0)
        self.assertIsNone(closed)
        self.assertEqual(state["positions"]["AAA"]["shares"], 6)
        self.assertAlmostEqual(state["positions"]["AAA"]["realized_pnl_usd"], 120.0)

    def test_full_sell_closes_position_and_reports_result(self):
        state = ai_ledger.apply_buy_fill(make_state(), "AAA", 10, 100.0, "2026-08-28")
        state, closed = ai_ledger.apply_sell_fill(state, "AAA", 10, 130.0)
        self.assertNotIn("AAA", state["positions"])
        self.assertAlmostEqual(closed["realized_pnl_usd"], 300.0)
        self.assertAlmostEqual(closed["return_pct"], 30.0)
        self.assertEqual(closed["first_entry_date"], "2026-08-28")


class TestBenchmarkIsFixed(unittest.TestCase):
    """勝敗の基準線（初期資金・ベンチマーク定義）が仕様どおりであること。"""

    def test_initial_capital_is_100k(self):
        self.assertEqual(config.AI_INITIAL_CAPITAL_USD, 100_000.0)

    def test_bench_units_are_capital_divided_by_voo_close(self):
        state = make_state()
        state["bench_units_ai"] = config.AI_INITIAL_CAPITAL_USD / 708.75
        self.assertAlmostEqual(ai_ledger.compute_bench_nav_usd(state, 708.75), 100_000.0, places=6)
        # VOOが10%上がればベンチマークも10%上がる（単位数は不変）
        self.assertAlmostEqual(ai_ledger.compute_bench_nav_usd(state, 779.625), 110_000.0, places=4)


class TestPromptsDoNotContainTradingRules(unittest.TestCase):
    """指示書「『RSIで絞れ』等の条件を指示書に書き込まないこと」の担保。

    プロンプトに具体的な売買ルール（買いの閾値・利確幅・損切り幅）が
    紛れ込んでいないことを機械的に見張る。
    """

    def test_no_hardcoded_thresholds_in_system_prompts(self):
        import ai_agents
        prompts = " ".join([
            ai_agents.SCOUT_PLAN_SYSTEM, ai_agents.SCOUT_PICK_SYSTEM,
            ai_agents.CHALLENGER_SYSTEM, ai_agents.EXECUTOR_SYSTEM,
        ])
        for banned in ("RSIが", "RSI(14)", "移動平均線を下回", "利確", "損切り", "％以上下落", "%以上下落"):
            self.assertNotIn(banned, prompts, f"プロンプトに売買ルール『{banned}』が書き込まれている")


if __name__ == "__main__":
    unittest.main()
