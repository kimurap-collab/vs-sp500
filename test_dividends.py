"""dividends.py・broker.get_dividends・jp_market.get_dividends・
rsi_daily.credit_dividends・jp_rsi_daily.credit_dividends_jpの単体テスト（2026-10-07・Change3）。

実データ・moomoo/yfinance接続を一切使わず、作ったtrades.csv行・配当履歴だけで判定する。
実行: python3 -m unittest test_dividends.py -v
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

import broker
import dividends
import jp_market
import jp_rsi_daily
import rsi_daily
import rsi_strategy


def _trade(date, action, shares, lot_id="TST-1", ticker="TST"):
    return {"date": date, "action": action, "shares": str(shares), "ticker": ticker, "lot_id": lot_id}


class TestSharesHeldBeforeDate(unittest.TestCase):
    """ex_date前日終値時点の保有株数を求める規約（境界含む）。"""

    def test_counts_buys_and_sells_strictly_before_ex_date(self):
        trades = [_trade("2026-01-05", "BUY", 300), _trade("2026-01-10", "BUY", 146)]
        self.assertEqual(dividends.shares_held_before_date(trades, "2026-01-15"), 446)

    def test_sell_before_ex_date_reduces_holding(self):
        trades = [
            _trade("2026-01-05", "BUY", 300),
            _trade("2026-01-10", "SELL", 150),
        ]
        self.assertEqual(dividends.shares_held_before_date(trades, "2026-01-15"), 150)

    def test_trade_on_ex_date_itself_not_counted(self):
        """ex_date当日の約定はまだ反映しない（前日終値基準）。"""
        trades = [_trade("2026-01-05", "BUY", 300), _trade("2026-01-15", "BUY", 100)]
        self.assertEqual(dividends.shares_held_before_date(trades, "2026-01-15"), 300)

    def test_entry_same_day_as_ex_date_holds_zero(self):
        trades = [_trade("2026-01-15", "BUY", 300)]
        self.assertEqual(dividends.shares_held_before_date(trades, "2026-01-15"), 0)


class TestComputeNewDividendCredits(unittest.TestCase):
    """検証: 保有株数×per_shareの算術、未保有・既記帳・データ無しの除外。"""

    def test_backfill_arithmetic(self):
        lots = [{"ticker": "TST", "lot_id": "TST-1"}]
        trades = [_trade("2026-01-05", "BUY", 300)]
        dividends_by_ticker = {"TST": [("2026-02-01", 2.5)]}
        rows = dividends.compute_new_dividend_credits(lots, trades, dividends_by_ticker, set(), "moomoo")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["shares"], 300)
        self.assertEqual(row["per_share"], 2.5)
        self.assertEqual(row["amount"], 750.0)
        self.assertEqual(row["source"], "moomoo")

    def test_idempotent_skips_already_credited_key(self):
        lots = [{"ticker": "TST", "lot_id": "TST-1"}]
        trades = [_trade("2026-01-05", "BUY", 300)]
        dividends_by_ticker = {"TST": [("2026-02-01", 2.5)]}
        existing_keys = {("TST", "2026-02-01", "TST-1")}
        rows = dividends.compute_new_dividend_credits(lots, trades, dividends_by_ticker, existing_keys, "moomoo")
        self.assertEqual(rows, [])

    def test_no_holding_before_ex_date_skipped(self):
        lots = [{"ticker": "TST", "lot_id": "TST-1"}]
        trades = [_trade("2026-03-01", "BUY", 300)]  # ex_dateより後に初めて買った
        dividends_by_ticker = {"TST": [("2026-02-01", 2.5)]}
        rows = dividends.compute_new_dividend_credits(lots, trades, dividends_by_ticker, set(), "moomoo")
        self.assertEqual(rows, [])

    def test_zero_or_negative_per_share_skipped(self):
        lots = [{"ticker": "TST", "lot_id": "TST-1"}]
        trades = [_trade("2026-01-05", "BUY", 300)]
        dividends_by_ticker = {"TST": [("2026-02-01", 0.0), ("2026-03-01", -1.0)]}
        rows = dividends.compute_new_dividend_credits(lots, trades, dividends_by_ticker, set(), "moomoo")
        self.assertEqual(rows, [])

    def test_multiple_lots_same_ticker_credited_independently(self):
        lots = [
            {"ticker": "TST", "lot_id": "TST-1"},
            {"ticker": "TST", "lot_id": "TST-2"},
        ]
        trades = [
            _trade("2026-01-05", "BUY", 300, lot_id="TST-1"),
            _trade("2026-01-20", "BUY", 100, lot_id="TST-2"),
        ]
        dividends_by_ticker = {"TST": [("2026-02-01", 2.5)]}
        rows = dividends.compute_new_dividend_credits(lots, trades, dividends_by_ticker, set(), "moomoo")
        amounts = {r["lot_id"]: r["amount"] for r in rows}
        self.assertEqual(amounts, {"TST-1": 750.0, "TST-2": 250.0})

    def test_closed_lot_still_credited_for_its_holding_period(self):
        """クローズ済みロットも保有していた期間分は記帳対象（バックフィル要件）。"""
        lots = [{"ticker": "TST", "lot_id": "TST-1", "closed": True}]
        trades = [
            _trade("2026-01-05", "BUY", 300),
            _trade("2026-02-10", "SELL", 300),  # ex_dateより後に売却
        ]
        dividends_by_ticker = {"TST": [("2026-02-01", 2.5)]}
        rows = dividends.compute_new_dividend_credits(lots, trades, dividends_by_ticker, set(), "moomoo")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["amount"], 750.0)

    def test_no_dividend_events_for_ticker_returns_empty(self):
        lots = [{"ticker": "TST", "lot_id": "TST-1"}]
        trades = [_trade("2026-01-05", "BUY", 300)]
        rows = dividends.compute_new_dividend_credits(lots, trades, {}, set(), "moomoo")
        self.assertEqual(rows, [])


class _FakeDivQuoteCtx:
    """moomoo OpenQuoteContext.get_rehabのper_cash_div/special_dividend応答を模したもの。"""

    def __init__(self, *args, **kwargs):
        pass

    def get_rehab(self, code):
        nan = float("nan")
        rows = {
            "US.TST": [
                {
                    "ex_div_date": "2026-02-01", "split_base": nan, "split_ert": nan,
                    "join_base": nan, "join_ert": nan, "per_cash_div": 0.5, "special_dividend": nan,
                },
                {
                    "ex_div_date": "2026-05-01", "split_base": nan, "split_ert": nan,
                    "join_base": nan, "join_ert": nan, "per_cash_div": 0.5, "special_dividend": 1.0,
                },
                {  # 配当の無い行（分割だけ）は無視される
                    "ex_div_date": "2026-06-10", "split_base": 1.0, "split_ert": 2.0,
                    "join_base": nan, "join_ert": nan, "per_cash_div": nan, "special_dividend": nan,
                },
            ],
        }
        if code not in rows:
            return -1, "unknown stock"
        return 0, pd.DataFrame(rows[code])

    def close(self):
        pass


class TestBrokerGetDividends(unittest.TestCase):
    def test_parses_per_cash_div_and_special_dividend(self):
        with patch("moomoo.OpenQuoteContext", _FakeDivQuoteCtx):
            result = broker.get_dividends(["TST", "XXX"])
        self.assertEqual(result["TST"], [("2026-02-01", 0.5), ("2026-05-01", 1.5)])
        self.assertNotIn("XXX", result)  # 個別失敗はその銘柄だけ欠ける


class TestJpMarketGetDividends(unittest.TestCase):
    def test_parses_yfinance_dividends(self):
        idx = pd.DatetimeIndex(["2026-02-01", "2026-05-01"]).tz_localize("Asia/Tokyo")
        fake = type("T", (), {"dividends": pd.Series([10.0, 12.0], index=idx)})
        with patch.object(jp_market.yf, "Ticker", return_value=fake):
            self.assertEqual(jp_market.get_dividends("6367"), [("2026-02-01", 10.0), ("2026-05-01", 12.0)])

    def test_exception_returns_none(self):
        with patch.object(jp_market.yf, "Ticker", side_effect=RuntimeError("boom")):
            self.assertIsNone(jp_market.get_dividends("6367"))


class TestCreditDividendsMissingDataWarns(unittest.TestCase):
    """データ取得失敗時はWARNINGを出し、記帳なしで続行すること（missing-data warning path）。"""

    def test_us_moomoo_failure_warns_and_skips(self):
        lot = rsi_strategy.new_lot("TST", "TST-1", "2026-01-05", 300, 100.0)
        state = {"lots": [lot], "cash_usd": 1000.0}
        log_lines: list[str] = []
        with patch("rsi_daily.broker.get_dividends", return_value=None), \
             self.assertLogs("vs-sp500.rsi_daily", level="WARNING") as cm:
            rsi_daily.credit_dividends(state, log_lines)
        self.assertTrue(any("配当情報が取得できず" in m for m in cm.output))
        self.assertEqual(state["cash_usd"], 1000.0)  # 変化なし

    def test_jp_yfinance_failure_warns_and_skips(self):
        lot = rsi_strategy.new_lot("6367", "6367-1", "2026-01-05", 100, 1000.0, lot_size=100)
        state = {"lots": [lot], "cash_jpy": 1_000_000.0}
        log_lines: list[str] = []
        with patch("jp_rsi_daily.jp_market.get_dividends", return_value=None), \
             self.assertLogs("vs-sp500.jp_rsi_daily", level="WARNING") as cm:
            jp_rsi_daily.credit_dividends_jp(state, log_lines)
        self.assertTrue(any("配当情報が取得できず" in m for m in cm.output))
        self.assertEqual(state["cash_jpy"], 1_000_000.0)  # 変化なし

    def test_us_credits_cash_and_writes_row_on_success(self):
        lot = rsi_strategy.new_lot("TST", "TST-1", "2026-01-05", 300, 100.0)
        state = {"lots": [lot], "cash_usd": 1000.0}
        log_lines: list[str] = []
        with patch("rsi_daily.broker.get_dividends", return_value={"TST": [("2026-02-01", 2.5)]}), \
             patch("rsi_daily.rsi_ledger.read_dividend_rows", return_value=[]), \
             patch("rsi_daily.rsi_ledger.read_trade_rows", return_value=[_trade("2026-01-05", "BUY", 300)]), \
             patch("rsi_daily.rsi_ledger.append_dividend_row") as mock_append:
            rsi_daily.credit_dividends(state, log_lines)
        self.assertEqual(state["cash_usd"], 1000.0 + 750.0)
        mock_append.assert_called_once()
        self.assertTrue(any("RSI-DIV" in line for line in log_lines))


if __name__ == "__main__":
    unittest.main()
