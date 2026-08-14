"""vs-sp500: 台帳I/O・評価額計算・ガードレール検証・約定処理。"""
from __future__ import annotations

import csv
import json
import logging
import re
from pathlib import Path
from typing import Any

import broker
import config
from market import TickerSnapshot

logger = logging.getLogger("vs-sp500.portfolio")

DEFAULT_PORTFOLIO: dict[str, Any] = {
    "start_date": None,
    "mode": "normal",
    "cash_usd": config.INITIAL_CAPITAL_USD,
    "holdings": {},
    "bench_units": 0.0,
    "last_processed_voo_date": None,
    "below_200dma_streak": 0,
    "above_200dma_streak": 0,
}

TRADES_CSV_HEADER = [
    "date", "action", "ticker", "shares", "price", "currency",
    "amount_usd", "fee_usd", "rule", "note",
]
HISTORY_CSV_HEADER = ["date", "nav_usd", "bench_usd", "diff_usd", "diff_pct", "cash_ratio"]


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
    """trades.csvから総平均法で各銘柄の平均取得単価（USD）を計算する。

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
# 評価額計算（v1.4: ドル建て。為替換算は一切行わない）
# ---------------------------------------------------------------------------

def compute_nav_usd(state: dict[str, Any], market: dict[str, TickerSnapshot]) -> float:
    nav = state["cash_usd"]
    for ticker, shares in state["holdings"].items():
        snap = market.get(ticker)
        if snap is None:
            continue
        nav += shares * snap.close
    return nav


def compute_bench_nav_usd(state: dict[str, Any], voo_close: float) -> float:
    """bench_unitsはVOOの株数なので通貨に依存しない。値は一切変更しないこと。"""
    return state["bench_units"] * voo_close


def compute_ticker_weight(ticker: str, state: dict[str, Any], market: dict[str, TickerSnapshot],
                           nav_usd: float) -> float:
    if nav_usd <= 0:
        return 0.0
    shares = state["holdings"].get(ticker, 0.0)
    snap = market.get(ticker)
    if snap is None or shares == 0.0:
        return 0.0
    return shares * snap.close / nav_usd


def compute_cash_ratio(state: dict[str, Any], nav_usd: float) -> float:
    if nav_usd <= 0:
        return 0.0
    return state["cash_usd"] / nav_usd


# ---------------------------------------------------------------------------
# 約定処理・ガードレール
# ---------------------------------------------------------------------------

class TradeRejected(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _max_weight_for_ticker(ticker: str) -> float:
    return config.MAX_VOO_WEIGHT if ticker == config.BENCHMARK_TICKER else config.MAX_TICKER_WEIGHT


def compute_stock_total_weight(state: dict[str, Any], market: dict[str, TickerSnapshot],
                                nav_usd: float) -> float:
    """個別株（ETF以外）の合計ウェイト。"""
    return sum(
        compute_ticker_weight(ticker, state, market, nav_usd)
        for ticker in state["holdings"]
        if config.is_stock(ticker)
    )


def reconcile_positions(state: dict[str, Any]) -> tuple[bool, str]:
    """台帳の保有とmoomooの実保有を突き合わせる。全銘柄一致でTrue。"""
    broker_positions = broker.get_positions()
    if broker_positions is None:
        return False, "moomooの保有取得に失敗した"

    ledger_holdings = {t: s for t, s in state.get("holdings", {}).items() if abs(s) > 1e-6}
    broker_holdings = {t: q for t, q in broker_positions.items() if abs(q) > 1e-6}

    ticker_mismatch = set(ledger_holdings) ^ set(broker_holdings)
    if ticker_mismatch:
        return False, f"保有銘柄が不一致（台帳のみ/moomooのみ）: {sorted(ticker_mismatch)}"

    qty_mismatches = [
        f"{ticker}(台帳{ledger_holdings[ticker]:g} vs moomoo{broker_holdings[ticker]:g})"
        for ticker in ledger_holdings
        if abs(ledger_holdings[ticker] - broker_holdings[ticker]) > 1e-6
    ]
    if qty_mismatches:
        return False, "株数不一致: " + ", ".join(qty_mismatches)

    return True, "一致"


def _pre_trade_guardrail_check(
    ticker: str,
    action: str,
    tentative_state: dict[str, Any],
    market: dict[str, TickerSnapshot],
    charter_targets: dict[str, dict[str, float]] | None,
    is_target_directed: bool,
) -> None:
    """見積り後のtentative_stateに対してターゲット超過・集中規制・現金比率をチェックする。

    違反時はTradeRejectedを送出する（この時点ではまだ実発注していない）。
    """
    if action == "BUY" and charter_targets and ticker in charter_targets:
        tmp_nav = compute_nav_usd(tentative_state, market)
        new_weight = compute_ticker_weight(ticker, tentative_state, market, tmp_nav)
        target_key = "defense" if tentative_state.get("mode") == "defense" else "normal"
        target_weight = charter_targets[ticker].get(target_key, 0.0)
        if new_weight > target_weight + config.TARGET_OVERSHOOT_TOLERANCE and is_target_directed:
            raise TradeRejected("ターゲット配分を超過する買い注文")

    if tentative_state["cash_usd"] < 0:
        raise TradeRejected("現金がマイナスになる注文")

    post_nav = compute_nav_usd(tentative_state, market)
    post_weight = compute_ticker_weight(ticker, tentative_state, market, post_nav)
    if config.is_stock(ticker):
        if post_weight > config.MAX_STOCK_WEIGHT + config.TARGET_WEIGHT_TOLERANCE:
            raise TradeRejected(f"個別株1銘柄上限超過（{ticker}: {post_weight:.1%}）")
        stock_total_weight = compute_stock_total_weight(tentative_state, market, post_nav)
        if stock_total_weight > config.MAX_STOCK_TOTAL_WEIGHT + config.TARGET_WEIGHT_TOLERANCE:
            raise TradeRejected(f"個別株合計上限超過（{stock_total_weight:.1%}）")
    else:
        if post_weight > _max_weight_for_ticker(ticker) + config.TARGET_WEIGHT_TOLERANCE:
            raise TradeRejected(f"銘柄集中規制超過（{ticker}: {post_weight:.1%}）")

    cash_ratio = compute_cash_ratio(tentative_state, post_nav)
    if cash_ratio < config.MIN_CASH_RATIO - config.TARGET_WEIGHT_TOLERANCE:
        raise TradeRejected("現金比率が下限(2%)を下回る")


def execute_trades(
    proposed_trades: list[dict[str, Any]],
    state: dict[str, Any],
    market: dict[str, TickerSnapshot],
    charter_targets: dict[str, dict[str, float]] | None,
    trade_date: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """提案された取引をガードレール検証のうえmoomooに実発注する。

    ガードレールは「見積り（現在値×想定株数）」で発注前に検証する（発注後は取り消せないため）。
    検証を通過したら実際にbroker.place_market_orderへ委譲し、実約定株数・実約定価格・
    moomooの現金増減（＝発注前後のbroker.get_cash()の差分）をそのまま台帳に反映する。

    戻り値: (新しいstate, 約定した取引ログ, 拒否された取引ログ)
    """
    new_state = json.loads(json.dumps(state))
    # SELLを先に約定して現金を作ってからBUYを処理する（リバランス時の現金不足による誤拒否を防ぐ）
    proposed_trades = sorted(proposed_trades, key=lambda t: 0 if t.get("action") == "SELL" else 1)
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    nav_usd = compute_nav_usd(new_state, market)
    non_target_used_usd = 0.0
    trade_count = 0

    for trade in proposed_trades:
        # 拒否時にこの取引の変更だけを巻き戻すためのスナップショット
        trade_snapshot = json.loads(json.dumps(new_state))
        try:
            if trade_count >= config.MAX_DAILY_TRADES:
                raise TradeRejected("1日の取引件数上限(10件)を超過")

            action = trade.get("action")
            ticker = trade.get("ticker")
            amount_usd = trade.get("amount_usd")
            rule = trade.get("rule")

            if ticker not in config.WHITELIST:
                raise TradeRejected(f"ホワイトリスト外の銘柄: {ticker}")
            if action not in ("BUY", "SELL"):
                raise TradeRejected(f"不正なaction: {action}")
            if not isinstance(amount_usd, (int, float)) or amount_usd <= 0:
                raise TradeRejected("amount_usdが不正")

            snap = market.get(ticker)
            if snap is None:
                raise TradeRejected(f"{ticker}の市場データが無い")

            is_target_directed = rule in ("rebalance", "defense_switch", "defense_return", "initial_build")
            is_forced_stop_loss = rule == "stop_loss"

            # 非ターゲット取引（押し目買い等）は1日あたりNAVの10%・現金の半分まで
            # （損切りはコード側の強制執行であり対象外。charter.md「個別株の追加ルール」準拠）
            if not is_target_directed and not is_forced_stop_loss:
                if amount_usd > nav_usd * config.NON_TARGET_TRADE_DAILY_CAP_OF_NAV - non_target_used_usd:
                    raise TradeRejected("非ターゲット取引の1日上限(評価額10%)を超過")
                if amount_usd > new_state["cash_usd"] * config.DIP_BUY_MAX_OF_CASH:
                    raise TradeRejected("押し目買いは現金の半分までしか許可されない")

            # 個別株の追加ガードレール（charter.md「個別株を持つ場合の追加ルール」準拠）
            if config.is_stock(ticker) and action == "BUY" and rule == "rebalance":
                raise TradeRejected("個別株はリバランスで買い増さない")

            currency = config.WHITELIST[ticker]["currency"]
            # 株数は整数のみ。金額から株数を求める際は切り捨て（浮動小数誤差対策で微小イプシロンを加算）
            qty = int(amount_usd / snap.close + 1e-6)

            if action == "BUY":
                if qty <= 0:
                    raise TradeRejected("金額が小さすぎて1株も買えない")
                estimated_cost = qty * snap.close
                if estimated_cost > new_state["cash_usd"] + 1e-6:
                    raise TradeRejected("現金不足（見積りでcash_usdを超過）")

                tentative_holdings = dict(new_state["holdings"])
                tentative_holdings[ticker] = tentative_holdings.get(ticker, 0.0) + qty
                tentative_state = {
                    **new_state, "holdings": tentative_holdings,
                    "cash_usd": new_state["cash_usd"] - estimated_cost,
                }
                _pre_trade_guardrail_check(ticker, action, tentative_state, market, charter_targets, is_target_directed)

                cash_before = broker.get_cash()
                fill = broker.place_market_order(ticker, qty, "BUY")
                if fill is None:
                    raise TradeRejected(
                        "moomoo発注が失敗または未約定確認"
                        "（実際に約定していた場合は翌朝reconcile_positionsが不一致として検知する）"
                    )
                filled_qty = fill["filled_qty"]
                avg_price = fill["avg_price"]
                cash_after = broker.get_cash()
                if cash_before is not None and cash_after is not None:
                    cash_delta = cash_after - cash_before
                else:
                    logger.warning("BUY %s: moomoo現金取得に失敗したため約定額から見積もった（fee不明）", ticker)
                    cash_delta = -(filled_qty * avg_price)

                new_state["cash_usd"] += cash_delta
                new_state["holdings"][ticker] = new_state["holdings"].get(ticker, 0.0) + filled_qty
                fee_usd = round(-cash_delta - filled_qty * avg_price, 4)
                shares, price_for_log = filled_qty, avg_price

            else:  # SELL
                held = new_state["holdings"].get(ticker, 0.0)
                qty = min(qty, int(held + 1e-6))
                if qty <= 0:
                    raise TradeRejected("保有株数を超えるSELL注文、または売却できる株数が無い")

                tentative_holdings = dict(new_state["holdings"])
                tentative_holdings[ticker] = held - qty
                if tentative_holdings[ticker] < 1e-9:
                    del tentative_holdings[ticker]
                estimated_proceeds = qty * snap.close
                tentative_state = {
                    **new_state, "holdings": tentative_holdings,
                    "cash_usd": new_state["cash_usd"] + estimated_proceeds,
                }
                _pre_trade_guardrail_check(ticker, action, tentative_state, market, charter_targets, is_target_directed)

                cash_before = broker.get_cash()
                fill = broker.place_market_order(ticker, qty, "SELL")
                if fill is None:
                    raise TradeRejected(
                        "moomoo発注が失敗または未約定確認"
                        "（実際に約定していた場合は翌朝reconcile_positionsが不一致として検知する）"
                    )
                filled_qty = fill["filled_qty"]
                avg_price = fill["avg_price"]
                cash_after = broker.get_cash()
                if cash_before is not None and cash_after is not None:
                    cash_delta = cash_after - cash_before
                else:
                    logger.warning("SELL %s: moomoo現金取得に失敗したため約定額から見積もった（fee不明）", ticker)
                    cash_delta = filled_qty * avg_price

                new_state["cash_usd"] += cash_delta
                new_state["holdings"][ticker] = held - filled_qty
                if new_state["holdings"][ticker] < 1e-9:
                    del new_state["holdings"][ticker]
                fee_usd = round(filled_qty * avg_price - cash_delta, 4)
                shares, price_for_log = filled_qty, avg_price

            if not is_target_directed and not is_forced_stop_loss:
                non_target_used_usd += amount_usd
            trade_count += 1

            accepted.append({
                "date": trade_date, "action": action, "ticker": ticker,
                "shares": shares, "price": round(price_for_log, 4), "currency": currency,
                "amount_usd": round(shares * price_for_log, 2), "fee_usd": fee_usd,
                "rule": rule, "note": "",
            })

        except TradeRejected as e:
            new_state = trade_snapshot  # この取引の変更を巻き戻す
            rejected.append({**trade, "reason": e.reason})

    return new_state, accepted, rejected


def check_stop_losses(state: dict[str, Any], market: dict[str, TickerSnapshot]) -> list[dict[str, Any]]:
    """個別株のうち平均取得単価比が STOCK_STOP_LOSS を下回るものの全売却注文を返す。"""
    avg_costs = compute_avg_costs()
    orders: list[dict[str, Any]] = []
    for ticker, shares in state["holdings"].items():
        if not config.is_stock(ticker) or shares <= 0:
            continue
        avg_cost = avg_costs.get(ticker)
        snap = market.get(ticker)
        if not avg_cost or snap is None:
            continue
        change = (snap.close - avg_cost) / avg_cost
        if change <= config.STOCK_STOP_LOSS:
            amount_usd = shares * snap.close
            orders.append({
                "action": "SELL", "ticker": ticker, "amount_usd": amount_usd,
                "rule": "stop_loss", "loss_pct": change,
            })
    return orders


def apply_dividends(state: dict[str, Any], market: dict[str, TickerSnapshot]) -> dict[str, Any]:
    """保有銘柄の当日配当を現金(USD)に加算する。"""
    new_state = json.loads(json.dumps(state))
    for ticker, shares in state["holdings"].items():
        snap = market.get(ticker)
        if snap is None or snap.dividend <= 0:
            continue
        new_state["cash_usd"] += shares * snap.dividend
    return new_state


def apply_benchmark_dividend(state: dict[str, Any], voo_snap: TickerSnapshot) -> dict[str, Any]:
    """ベンチマークのVOO配当をVOO口数に再投資する。"""
    if voo_snap.dividend <= 0 or state["bench_units"] <= 0:
        return state
    new_state = json.loads(json.dumps(state))
    new_state["bench_units"] += state["bench_units"] * voo_snap.dividend / voo_snap.close
    return new_state
