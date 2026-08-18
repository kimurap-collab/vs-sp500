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
    "pending_orders": [],
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
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """提案された取引をガードレール検証のうえmoomooに実発注する。

    ガードレールは「見積り（現在値×想定株数）」で発注前に検証する（発注後は取り消せないため）。
    検証を通過したら実際にbroker.place_market_orderへ委譲する。その場で全量約定したら
    従来どおり即座に台帳へ反映する（現金増減は発注前後のbroker.get_cash()の差分）。
    その場で約定しなかった分（未約定・一部約定の残り）は台帳を変更せずpending_ordersに記録し、
    次回実行の冒頭（settle_pending_orders）で決済する。保有照合は行わない
    （各枠が自分の出したpending_ordersだけの面倒を見る。2026-08-18仕様変更）。

    戻り値: (新しいstate, 約定した取引ログ, 拒否された取引ログ, 未決注文としてキューに積んだログ)
    """
    new_state = json.loads(json.dumps(state))
    # SELLを先に約定して現金を作ってからBUYを処理する（リバランス時の現金不足による誤拒否を防ぐ）
    proposed_trades = sorted(proposed_trades, key=lambda t: 0 if t.get("action") == "SELL" else 1)
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    queued: list[dict[str, Any]] = []
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
                    raise TradeRejected("moomoo発注の送信に失敗した")
                trade_count += 1
                if not is_target_directed and not is_forced_stop_loss:
                    non_target_used_usd += amount_usd

                outcome = broker.classify_fill(qty, fill)
                filled_qty = fill["filled_qty"]
                avg_price = fill["avg_price"]

                if outcome == "NONE_TERMINAL":
                    raise TradeRejected(f"moomoo注文が約定せず終端した（status={fill['status']}）")

                if outcome == "NONE_OPEN":
                    # その場で1株も約定しなかった。台帳は一切変更せず全量をpending_ordersへ記録する
                    new_state.setdefault("pending_orders", []).append({
                        "order_id": fill["order_id"], "ticker": ticker, "side": "BUY", "qty": qty,
                        "submitted_date": trade_date, "applied_qty": 0, "applied_value_usd": 0.0,
                        "rule": rule,
                    })
                    queued.append({
                        "date": trade_date, "action": "BUY", "ticker": ticker, "qty": qty,
                        "order_id": fill["order_id"], "rule": rule,
                    })
                    continue

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
                note = ""

                if outcome == "PARTIAL_OPEN":
                    new_state.setdefault("pending_orders", []).append({
                        "order_id": fill["order_id"], "ticker": ticker, "side": "BUY", "qty": qty,
                        "submitted_date": trade_date, "applied_qty": filled_qty,
                        "applied_value_usd": filled_qty * avg_price, "rule": rule,
                    })
                    note = f"一部約定・残{qty - filled_qty}株はpending_orders追跡"
                elif outcome == "PARTIAL_TERMINAL":
                    logger.warning(
                        "BUY %s: 一部約定(%d/%d株)のまま終端した（status=%s）。残数は打ち切り",
                        ticker, filled_qty, qty, fill["status"],
                    )
                    note = f"一部約定({filled_qty}/{qty}株)のまま終端・残数は打ち切り"

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
                    raise TradeRejected("moomoo発注の送信に失敗した")
                trade_count += 1
                if not is_target_directed and not is_forced_stop_loss:
                    non_target_used_usd += amount_usd

                outcome = broker.classify_fill(qty, fill)
                filled_qty = fill["filled_qty"]
                avg_price = fill["avg_price"]

                if outcome == "NONE_TERMINAL":
                    raise TradeRejected(f"moomoo注文が約定せず終端した（status={fill['status']}）")

                if outcome == "NONE_OPEN":
                    new_state.setdefault("pending_orders", []).append({
                        "order_id": fill["order_id"], "ticker": ticker, "side": "SELL", "qty": qty,
                        "submitted_date": trade_date, "applied_qty": 0, "applied_value_usd": 0.0,
                        "rule": rule,
                    })
                    queued.append({
                        "date": trade_date, "action": "SELL", "ticker": ticker, "qty": qty,
                        "order_id": fill["order_id"], "rule": rule,
                    })
                    continue

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
                note = ""

                if outcome == "PARTIAL_OPEN":
                    new_state.setdefault("pending_orders", []).append({
                        "order_id": fill["order_id"], "ticker": ticker, "side": "SELL", "qty": qty,
                        "submitted_date": trade_date, "applied_qty": filled_qty,
                        "applied_value_usd": filled_qty * avg_price, "rule": rule,
                    })
                    note = f"一部約定・残{qty - filled_qty}株はpending_orders追跡"
                elif outcome == "PARTIAL_TERMINAL":
                    logger.warning(
                        "SELL %s: 一部約定(%d/%d株)のまま終端した（status=%s）。残数は打ち切り",
                        ticker, filled_qty, qty, fill["status"],
                    )
                    note = f"一部約定({filled_qty}/{qty}株)のまま終端・残数は打ち切り"

            accepted.append({
                "date": trade_date, "action": action, "ticker": ticker,
                "shares": shares, "price": round(price_for_log, 4), "currency": currency,
                "amount_usd": round(shares * price_for_log, 2), "fee_usd": fee_usd,
                "rule": rule, "note": note,
            })

        except TradeRejected as e:
            new_state = trade_snapshot  # この取引の変更を巻き戻す
            rejected.append({**trade, "reason": e.reason})

    return new_state, accepted, rejected, queued


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


# ---------------------------------------------------------------------------
# 未決注文の決済（保有照合の代わり。各枠は自分のpending_ordersだけを見る。2026-08-18仕様変更）
# ---------------------------------------------------------------------------

def settle_pending_orders(state: dict[str, Any], today: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """本体のpending_ordersを毎回の実行冒頭でmoomooに問い合わせ、確定した分を台帳へ反映する。

    - 全量/一部が新たに約定していたら、その約定株数・約定価格で保有・現金・trades.csvへ反映する
      （決済は当日に限らず後日になるため、broker.get_cash()の前後差ではなく約定株数×約定単価で
      現金へ反映する。moomoo APIから手数料を取得する経路が無いため、fee_usdは0とし
      「手数料未計上」の旨をnoteとログに残す＝仕様の「現金の扱い」節の調査結果）。
    - 全量約定 or 終端ステータス（キャンセル等）ならpending_ordersから外す。終端の場合は警告を残す。
    - 問い合わせ自体が失敗（broker.get_order_statusがNone）した項目は変更せず残す。
    - まだ進行中（SUBMITTED等）の項目もそのまま残す。

    戻り値: (新しいstate, 反映した取引ログ, 警告メッセージのリスト)
    """
    new_state = json.loads(json.dumps(state))
    pending = list(new_state.get("pending_orders", []))
    remaining: list[dict[str, Any]] = []
    applied_trades: list[dict[str, Any]] = []
    warnings: list[str] = []

    for order in pending:
        info = broker.get_order_status(order["order_id"])
        if info is None:
            remaining.append(order)
            continue

        applied_qty = order.get("applied_qty", 0)
        applied_value = order.get("applied_value_usd", 0.0)
        dealt_qty = info["filled_qty"]
        dealt_avg_price = info["avg_price"]
        status = info["status"]

        new_fill_qty = dealt_qty - applied_qty
        if new_fill_qty > 0:
            total_value = dealt_qty * dealt_avg_price
            incremental_value = total_value - applied_value
            incremental_price = incremental_value / new_fill_qty
            fill_date = info.get("updated_date") or order["submitted_date"]
            ticker = order["ticker"]
            side = order["side"]

            if side == "BUY":
                new_state["holdings"][ticker] = new_state["holdings"].get(ticker, 0.0) + new_fill_qty
                new_state["cash_usd"] -= incremental_price * new_fill_qty
            else:
                new_state["holdings"][ticker] = new_state["holdings"].get(ticker, 0.0) - new_fill_qty
                if new_state["holdings"][ticker] < 1e-9:
                    del new_state["holdings"][ticker]
                new_state["cash_usd"] += incremental_price * new_fill_qty

            trade_row = {
                "date": fill_date, "action": side, "ticker": ticker,
                "shares": new_fill_qty, "price": round(incremental_price, 4),
                "currency": config.WHITELIST.get(ticker, {}).get("currency", "USD"),
                "amount_usd": round(new_fill_qty * incremental_price, 2), "fee_usd": 0.0,
                "rule": order.get("rule", ""),
                "note": f"pending決済(order_id={order['order_id']})・手数料不明のため未計上",
            }
            applied_trades.append(trade_row)
            warnings.append(
                f"pending決済: {ticker} {side} {new_fill_qty}株 @ {incremental_price:.4f}"
                f"（order_id={order['order_id']}・手数料は台帳に未計上）"
            )

            applied_qty = dealt_qty
            applied_value = total_value

        if status == "FILLED_ALL" or applied_qty >= order["qty"]:
            continue  # 全量確定。pending_ordersから外す

        if status in broker.ORDER_TERMINAL_STATUSES:
            if applied_qty < order["qty"]:
                warnings.append(
                    f"未決注文が未達のまま終端した（status={status}）: order_id={order['order_id']} "
                    f"{order['ticker']} 残数{order['qty'] - applied_qty}株は打ち切り"
                )
            continue  # 終端。pending_ordersから外す

        remaining.append({**order, "applied_qty": applied_qty, "applied_value_usd": applied_value})

    new_state["pending_orders"] = remaining
    return new_state, applied_trades, warnings


