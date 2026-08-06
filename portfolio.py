"""vs-sp500: 台帳I/O・評価額計算・ガードレール検証・約定処理。"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

import config
from market import TickerSnapshot

DEFAULT_PORTFOLIO: dict[str, Any] = {
    "start_date": None,
    "mode": "normal",
    "cash_jpy": config.INITIAL_CAPITAL_JPY,
    "cash_usd": 0.0,
    "holdings": {},
    "bench_units": 0.0,
    "last_processed_voo_date": None,
    "below_200dma_streak": 0,
    "above_200dma_streak": 0,
}

TRADES_CSV_HEADER = [
    "date", "action", "ticker", "shares", "price", "currency",
    "fx_rate", "amount_jpy", "fee_jpy", "rule", "note",
]
HISTORY_CSV_HEADER = ["date", "nav_jpy", "bench_jpy", "diff_jpy", "diff_pct", "cash_ratio"]


# ---------------------------------------------------------------------------
# 台帳I/O
# ---------------------------------------------------------------------------

def load_portfolio() -> dict[str, Any]:
    if not config.PORTFOLIO_PATH.exists():
        return json.loads(json.dumps(DEFAULT_PORTFOLIO))
    with open(config.PORTFOLIO_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_portfolio(state: dict[str, Any]) -> None:
    config.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = config.PORTFOLIO_PATH.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp_path.replace(config.PORTFOLIO_PATH)


def append_trade_row(row: dict[str, Any]) -> None:
    config.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    is_new = not config.TRADES_CSV_PATH.exists()
    with open(config.TRADES_CSV_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TRADES_CSV_HEADER)
        if is_new:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in TRADES_CSV_HEADER})


def append_history_row(row: dict[str, Any]) -> None:
    """同日の行があれば上書き、無ければ追記する。"""
    config.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    if config.HISTORY_CSV_PATH.exists():
        with open(config.HISTORY_CSV_PATH, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    rows = [r for r in rows if r.get("date") != row["date"]]
    rows.append({k: row.get(k, "") for k in HISTORY_CSV_HEADER})
    rows.sort(key=lambda r: r["date"])
    with open(config.HISTORY_CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=HISTORY_CSV_HEADER)
        writer.writeheader()
        writer.writerows(rows)


def read_history_rows() -> list[dict[str, Any]]:
    if not config.HISTORY_CSV_PATH.exists():
        return []
    with open(config.HISTORY_CSV_PATH, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def read_trade_rows() -> list[dict[str, Any]]:
    if not config.TRADES_CSV_PATH.exists():
        return []
    with open(config.TRADES_CSV_PATH, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def compute_avg_costs() -> dict[str, float]:
    """trades.csvから総平均法で各銘柄の平均取得単価（建て通貨）を計算する。

    BUYは加重平均で更新（単価は約定price、手数料は含めない）。
    SELLは株数を減らすだけで平均単価は変えない。
    """
    avg_cost: dict[str, float] = {}
    total_shares: dict[str, float] = {}
    for row in read_trade_rows():
        ticker = row["ticker"]
        shares = float(row["shares"])
        price = float(row["price"])
        prev_shares = total_shares.get(ticker, 0.0)
        if row["action"] == "BUY":
            new_shares = prev_shares + shares
            if new_shares > 0:
                prev_avg = avg_cost.get(ticker, 0.0)
                avg_cost[ticker] = (prev_avg * prev_shares + price * shares) / new_shares
            total_shares[ticker] = new_shares
        elif row["action"] == "SELL":
            total_shares[ticker] = prev_shares - shares
    return avg_cost


# ---------------------------------------------------------------------------
# charter.md ターゲット配分パース
# ---------------------------------------------------------------------------

def load_charter_text() -> str:
    return config.CHARTER_PATH.read_text(encoding="utf-8")


def parse_charter_targets(charter_text: str) -> dict[str, dict[str, float]] | None:
    """ターゲット配分テーブルをパースする。未記入ならNoneを返す。

    戻り値: {ticker_or_'現金': {"normal": 0.0-1.0, "defense": 0.0-1.0}}
    """
    section_match = re.search(r"## ターゲット配分(.*?)\n## ", charter_text, re.S)
    if not section_match:
        section_match = re.search(r"## ターゲット配分(.*)", charter_text, re.S)
    if not section_match:
        return None
    section = section_match.group(1)

    table_rows = [line.strip() for line in section.splitlines() if line.strip().startswith("|")]
    if len(table_rows) < 3:
        return None  # ヘッダ＋区切り＋データ行が無い

    data_rows = table_rows[2:]  # ヘッダ行・区切り行をスキップ
    targets: dict[str, dict[str, float]] = {}
    for row in data_rows:
        cells = [c.strip() for c in row.strip("|").split("|")]
        if len(cells) < 3:
            continue
        asset, normal_raw, defense_raw = cells[0], cells[1], cells[2]
        if "記入" in asset or not asset:
            return None  # プレースホルダ行のみ＝未記入
        normal = _pct_to_float(normal_raw)
        defense = _pct_to_float(defense_raw)
        if normal is None and defense is None:
            return None
        targets[asset] = {"normal": normal or 0.0, "defense": defense or 0.0}

    return targets if targets else None


def _pct_to_float(raw: str) -> float | None:
    raw = raw.strip().replace("%", "")
    if not raw or raw == "-":
        return None
    try:
        return float(raw) / 100.0
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 評価額計算
# ---------------------------------------------------------------------------

def compute_nav_jpy(state: dict[str, Any], market: dict[str, TickerSnapshot], usdjpy_mid: float) -> float:
    nav = state["cash_jpy"] + state["cash_usd"] * usdjpy_mid
    for ticker, shares in state["holdings"].items():
        snap = market.get(ticker)
        if snap is None:
            continue
        currency = config.WHITELIST[ticker]["currency"]
        if currency == "USD":
            nav += shares * snap.close * usdjpy_mid
        else:
            nav += shares * snap.close
    return nav


def compute_bench_nav_jpy(state: dict[str, Any], voo_close: float, usdjpy_mid: float) -> float:
    return state["bench_units"] * voo_close * usdjpy_mid


def compute_ticker_weight(ticker: str, state: dict[str, Any], market: dict[str, TickerSnapshot],
                           usdjpy_mid: float, nav_jpy: float) -> float:
    if nav_jpy <= 0:
        return 0.0
    shares = state["holdings"].get(ticker, 0.0)
    snap = market.get(ticker)
    if snap is None or shares == 0.0:
        return 0.0
    currency = config.WHITELIST[ticker]["currency"]
    value_jpy = shares * snap.close * (usdjpy_mid if currency == "USD" else 1.0)
    return value_jpy / nav_jpy


def compute_cash_ratio(state: dict[str, Any], usdjpy_mid: float, nav_jpy: float) -> float:
    if nav_jpy <= 0:
        return 0.0
    cash_jpy_equiv = state["cash_jpy"] + state["cash_usd"] * usdjpy_mid
    return cash_jpy_equiv / nav_jpy


# ---------------------------------------------------------------------------
# 約定処理・ガードレール
# ---------------------------------------------------------------------------

class TradeRejected(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _max_weight_for_ticker(ticker: str) -> float:
    return config.MAX_VOO_WEIGHT if ticker == config.BENCHMARK_TICKER else config.MAX_TICKER_WEIGHT


def _fee_and_shares_for_buy(ticker: str, amount_jpy: float, snap: TickerSnapshot, usdjpy_mid: float
                             ) -> tuple[float, float, float]:
    """BUY注文の (shares, fee_jpy, actual_amount_jpy) を返す（現金移動は呼び出し側で行う）。"""
    currency = config.WHITELIST[ticker]["currency"]
    if currency == "JPY":
        shares = amount_jpy / snap.close
        return shares, 0.0, amount_jpy
    # USD建て
    trade_value_usd = amount_jpy / usdjpy_mid
    shares = trade_value_usd / snap.close
    fee_usd = min(trade_value_usd * config.US_ETF_FEE_RATE, config.US_ETF_FEE_CAP_USD)
    fee_jpy = fee_usd * usdjpy_mid
    return shares, fee_jpy, trade_value_usd * usdjpy_mid


def execute_trades(
    proposed_trades: list[dict[str, Any]],
    state: dict[str, Any],
    market: dict[str, TickerSnapshot],
    usdjpy_mid: float,
    charter_targets: dict[str, dict[str, float]] | None,
    trade_date: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """提案された取引をガードレール検証のうえ約定する。

    戻り値: (新しいstate, 約定した取引ログ, 拒否された取引ログ)
    """
    new_state = json.loads(json.dumps(state))
    # SELLを先に約定して現金を作ってからBUYを処理する（リバランス時の現金不足による誤拒否を防ぐ）
    proposed_trades = sorted(proposed_trades, key=lambda t: 0 if t.get("action") == "SELL" else 1)
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    nav_jpy = compute_nav_jpy(new_state, market, usdjpy_mid)
    non_target_used_jpy = 0.0
    trade_count = 0

    for trade in proposed_trades:
        # 拒否時にこの取引の変更だけを巻き戻すためのスナップショット
        trade_snapshot = json.loads(json.dumps(new_state))
        try:
            if trade_count >= config.MAX_DAILY_TRADES:
                raise TradeRejected("1日の取引件数上限(10件)を超過")

            action = trade.get("action")
            ticker = trade.get("ticker")
            amount_jpy = trade.get("amount_jpy")
            rule = trade.get("rule")

            if ticker not in config.WHITELIST:
                raise TradeRejected(f"ホワイトリスト外の銘柄: {ticker}")
            if action not in ("BUY", "SELL"):
                raise TradeRejected(f"不正なaction: {action}")
            if not isinstance(amount_jpy, (int, float)) or amount_jpy <= 0:
                raise TradeRejected("amount_jpyが不正")

            snap = market.get(ticker)
            if snap is None:
                raise TradeRejected(f"{ticker}の市場データが無い")

            is_target_directed = rule in ("rebalance", "defense_switch", "defense_return", "initial_build")

            # 非ターゲット取引（押し目買い等）は1日あたりNAVの10%・現金の半分まで
            if not is_target_directed:
                if amount_jpy > nav_jpy * config.NON_TARGET_TRADE_DAILY_CAP_OF_NAV - non_target_used_jpy:
                    raise TradeRejected("非ターゲット取引の1日上限(評価額10%)を超過")
                cash_jpy_equiv = new_state["cash_jpy"] + new_state["cash_usd"] * usdjpy_mid
                if amount_jpy > cash_jpy_equiv * config.DIP_BUY_MAX_OF_CASH:
                    raise TradeRejected("押し目買いは現金の半分までしか許可されない")

            currency = config.WHITELIST[ticker]["currency"]

            if action == "BUY":
                shares, fee_jpy, actual_amount_jpy = _fee_and_shares_for_buy(ticker, amount_jpy, snap, usdjpy_mid)

                if currency == "USD":
                    trade_value_usd = actual_amount_jpy / usdjpy_mid
                    fee_usd = fee_jpy / usdjpy_mid
                    total_usd_needed = trade_value_usd + fee_usd
                    if new_state["cash_usd"] >= total_usd_needed:
                        new_state["cash_usd"] -= total_usd_needed
                    else:
                        shortfall_usd = total_usd_needed - new_state["cash_usd"]
                        jpy_cost = shortfall_usd * (usdjpy_mid + config.FX_SPREAD_JPY_PER_USD)
                        if new_state["cash_jpy"] < jpy_cost:
                            raise TradeRejected("現金不足（JPY・USD合計で不足）")
                        new_state["cash_jpy"] -= jpy_cost
                        new_state["cash_usd"] = 0.0
                    price_for_log = snap.close
                else:
                    if new_state["cash_jpy"] < amount_jpy:
                        raise TradeRejected("現金不足（JPY）")
                    new_state["cash_jpy"] -= amount_jpy
                    price_for_log = snap.close

                new_state["holdings"][ticker] = new_state["holdings"].get(ticker, 0.0) + shares

                # ターゲット超過チェック（ターゲットが定義されている場合のみ）
                if charter_targets and ticker in charter_targets:
                    tmp_nav = compute_nav_jpy(new_state, market, usdjpy_mid)
                    new_weight = compute_ticker_weight(ticker, new_state, market, usdjpy_mid, tmp_nav)
                    target_key = "defense" if new_state.get("mode") == "defense" else "normal"
                    target_weight = charter_targets[ticker].get(target_key, 0.0)
                    if new_weight > target_weight + config.TARGET_OVERSHOOT_TOLERANCE and is_target_directed:
                        raise TradeRejected("ターゲット配分を超過する買い注文")

                if new_state["cash_jpy"] < 0 or new_state["cash_usd"] < 0:
                    raise TradeRejected("現金がマイナスになる注文")

            else:  # SELL
                held = new_state["holdings"].get(ticker, 0.0)
                if currency == "USD":
                    shares = amount_jpy / (snap.close * usdjpy_mid)
                else:
                    shares = amount_jpy / snap.close
                if shares > held + 1e-9:
                    raise TradeRejected("保有株数を超えるSELL注文")

                proceeds_native = shares * snap.close
                if currency == "USD":
                    fee_native = min(proceeds_native * config.US_ETF_FEE_RATE, config.US_ETF_FEE_CAP_USD)
                    net_native = proceeds_native - fee_native
                    new_state["cash_usd"] += net_native
                    fee_jpy = fee_native * usdjpy_mid
                else:
                    fee_native = 0.0
                    new_state["cash_jpy"] += proceeds_native
                    fee_jpy = 0.0
                price_for_log = snap.close

                new_state["holdings"][ticker] = held - shares
                if new_state["holdings"][ticker] < 1e-9:
                    del new_state["holdings"][ticker]

            # 個別銘柄集中規制・現金比率規制（共通チェック）
            post_nav = compute_nav_jpy(new_state, market, usdjpy_mid)
            post_weight = compute_ticker_weight(ticker, new_state, market, usdjpy_mid, post_nav)
            if post_weight > _max_weight_for_ticker(ticker) + config.TARGET_WEIGHT_TOLERANCE:
                raise TradeRejected(f"銘柄集中規制超過（{ticker}: {post_weight:.1%}）")
            cash_ratio = compute_cash_ratio(new_state, usdjpy_mid, post_nav)
            if cash_ratio < config.MIN_CASH_RATIO - config.TARGET_WEIGHT_TOLERANCE:
                raise TradeRejected("現金比率が下限(2%)を下回る")

            if not is_target_directed:
                non_target_used_jpy += amount_jpy
            trade_count += 1

            accepted.append({
                "date": trade_date, "action": action, "ticker": ticker,
                "shares": round(shares, 6), "price": price_for_log, "currency": currency,
                "fx_rate": round(usdjpy_mid, 3), "amount_jpy": round(amount_jpy),
                "fee_jpy": round(fee_jpy), "rule": rule, "note": "",
            })

        except TradeRejected as e:
            new_state = trade_snapshot  # この取引の変更を巻き戻す
            rejected.append({**trade, "reason": e.reason})

    return new_state, accepted, rejected


def apply_dividends(state: dict[str, Any], market: dict[str, TickerSnapshot]) -> dict[str, Any]:
    """保有銘柄の当日配当を現金に加算する。"""
    new_state = json.loads(json.dumps(state))
    for ticker, shares in state["holdings"].items():
        snap = market.get(ticker)
        if snap is None or snap.dividend <= 0:
            continue
        currency = config.WHITELIST[ticker]["currency"]
        amount = shares * snap.dividend
        if currency == "USD":
            new_state["cash_usd"] += amount
        else:
            new_state["cash_jpy"] += amount
    return new_state


def apply_benchmark_dividend(state: dict[str, Any], voo_snap: TickerSnapshot) -> dict[str, Any]:
    """ベンチマークのVOO配当をVOO口数に再投資する。"""
    if voo_snap.dividend <= 0 or state["bench_units"] <= 0:
        return state
    new_state = json.loads(json.dumps(state))
    new_state["bench_units"] += state["bench_units"] * voo_snap.dividend / voo_snap.close
    return new_state
