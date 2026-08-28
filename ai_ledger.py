"""vs-sp500: AI裁量枠の台帳I/Oとガードレール（ledger/ai/ 配下）。

本体・RSI枠・日本株RSI枠とは別ファイル・別スキーマで完全に独立させる。
**この枠が触ってよいのは自分の台帳（positions）に載っている玉と現金だけ**であり、
口座全体の保有・他の枠の玉には一切触れない（charter.md「各枠は自分が出した注文だけを管理する」）。

売買ルールは持たない（それはAIが毎回決める）。ここに書くのは
「AIが何を言おうと通してはいけないもの」＝ガードレールだけである。
"""
from __future__ import annotations

import csv
import json
import logging
import math
from typing import Any

import config
from market import TickerSnapshot

logger = logging.getLogger("vs-sp500.ai_ledger")

DEFAULT_AI_PORTFOLIO: dict[str, Any] = {
    "start_date": None,
    "cash_usd": config.AI_INITIAL_CAPITAL_USD,
    "positions": {},          # ticker -> {shares, total_cost_usd, avg_cost, name, first_entry_date}
    "bench_units_ai": 0.0,
    "last_processed_date": None,
    "pending_orders": [],
    "orders_placed_today": {"date": None, "count": 0},
}

# reason列がこの枠の主眼（大将「buyとかsellとか書いてるところに理由書いてな」）。
# 一度書いた理由は後から書き換えない。
AI_TRADES_CSV_HEADER = [
    "date", "action", "ticker", "name", "shares", "price", "amount_usd", "rule", "reason", "note",
]
AI_HISTORY_CSV_HEADER = [
    "date", "nav_usd", "bench_usd", "diff_usd", "diff_pct", "cash_ratio", "positions",
]


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def load_portfolio() -> dict[str, Any]:
    if not config.AI_PORTFOLIO_PATH.exists():
        return json.loads(json.dumps(DEFAULT_AI_PORTFOLIO))
    with open(config.AI_PORTFOLIO_PATH, encoding="utf-8") as f:
        state = json.load(f)
    for key, default in DEFAULT_AI_PORTFOLIO.items():
        state.setdefault(key, json.loads(json.dumps(default)))
    return state


def save_portfolio(state: dict[str, Any]) -> None:
    config.AI_LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = config.AI_PORTFOLIO_PATH.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp_path.replace(config.AI_PORTFOLIO_PATH)


def append_trade_row(row: dict[str, Any]) -> None:
    config.AI_LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    is_new = not config.AI_TRADES_CSV_PATH.exists()
    with open(config.AI_TRADES_CSV_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=AI_TRADES_CSV_HEADER)
        if is_new:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in AI_TRADES_CSV_HEADER})


def append_history_row(row: dict[str, Any]) -> None:
    """同日の行があれば上書き、無ければ追記する（他の枠のappend_history_rowと同じ規約）。"""
    config.AI_LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    if config.AI_HISTORY_CSV_PATH.exists():
        with open(config.AI_HISTORY_CSV_PATH, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    rows = [r for r in rows if r.get("date") != row["date"]]
    rows.append({k: row.get(k, "") for k in AI_HISTORY_CSV_HEADER})
    rows.sort(key=lambda r: r["date"])
    with open(config.AI_HISTORY_CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=AI_HISTORY_CSV_HEADER)
        writer.writeheader()
        writer.writerows(rows)


def read_history_rows() -> list[dict[str, Any]]:
    if not config.AI_HISTORY_CSV_PATH.exists():
        return []
    with open(config.AI_HISTORY_CSV_PATH, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def read_trade_rows() -> list[dict[str, Any]]:
    if not config.AI_TRADES_CSV_PATH.exists():
        return []
    with open(config.AI_TRADES_CSV_PATH, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _append_jsonl(path, record: dict[str, Any]) -> None:
    config.AI_LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _read_jsonl(path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("%s に壊れた行があるため読み飛ばした", path.name)
    return records


def append_decision(record: dict[str, Any]) -> None:
    """その日の判断を1行追記する（何もしなかった日も必ず残す）。後から書き換えない。"""
    _append_jsonl(config.AI_DECISIONS_PATH, record)


def read_decisions() -> list[dict[str, Any]]:
    return _read_jsonl(config.AI_DECISIONS_PATH)


def append_review(record: dict[str, Any]) -> None:
    """建玉が無くなった取引の振り返りを1行追記する。"""
    _append_jsonl(config.AI_REVIEWS_PATH, record)


def read_reviews() -> list[dict[str, Any]]:
    return _read_jsonl(config.AI_REVIEWS_PATH)


# ---------------------------------------------------------------------------
# 集計
# ---------------------------------------------------------------------------

def open_positions(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {t: p for t, p in state.get("positions", {}).items() if p.get("shares", 0) > 0}


def compute_nav_usd(state: dict[str, Any], market: dict[str, TickerSnapshot]) -> float:
    nav = state["cash_usd"]
    for ticker, pos in open_positions(state).items():
        snap = market.get(ticker)
        if snap is None:
            continue
        nav += pos["shares"] * snap.close
    return nav


def compute_bench_nav_usd(state: dict[str, Any], voo_close: float) -> float:
    return state.get("bench_units_ai", 0.0) * voo_close


def compute_cash_ratio(state: dict[str, Any], nav_usd: float) -> float:
    if nav_usd <= 0:
        return 0.0
    return state["cash_usd"] / nav_usd


def compute_available_cash(
    state: dict[str, Any], market_prices: dict[str, float] | None = None,
) -> float:
    """新規注文に使える現金。自分の未決BUY注文で予約済みの額を差し引く（RSI枠と同じ方式）。

    他の枠のpending_ordersは見ない（互いの注文を見ない原則）。
    """
    reserved = 0.0
    for order in state.get("pending_orders", []):
        if order.get("side") != "BUY":
            continue
        remaining_qty = order["qty"] - order.get("applied_qty", 0)
        if remaining_qty <= 0:
            continue
        est_price = order.get("est_price")
        if est_price is None:
            est_price = (market_prices or {}).get(order["ticker"])
            if est_price is None:
                logger.warning(
                    "pending_orders(order_id=%s, %s)にest_priceが無く現在値も取得できないため、"
                    "現金予約額0として扱う", order.get("order_id"), order.get("ticker"),
                )
                est_price = 0.0
        reserved += remaining_qty * est_price
    return state["cash_usd"] - reserved


def reserved_sell_shares(state: dict[str, Any], ticker: str) -> float:
    """未決のSELL注文で既に売りに出している株数（二重に売らないため）。"""
    total = 0.0
    for order in state.get("pending_orders", []):
        if order.get("side") != "SELL" or order.get("ticker") != ticker:
            continue
        total += max(0, order["qty"] - order.get("applied_qty", 0))
    return total


def orders_placed_today(state: dict[str, Any], date: str) -> int:
    counter = state.get("orders_placed_today") or {}
    return counter.get("count", 0) if counter.get("date") == date else 0


# ---------------------------------------------------------------------------
# 玉の更新（純粋関数）
# ---------------------------------------------------------------------------

def apply_buy_fill(
    state: dict[str, Any], ticker: str, qty: int, price: float, date: str, name: str | None = None,
) -> dict[str, Any]:
    """BUY約定をpositionsへ反映した新しいstateを返す（現金は呼び出し側が実増減で更新する）。"""
    positions = {t: dict(p) for t, p in state.get("positions", {}).items()}
    pos = positions.get(ticker) or {
        "shares": 0.0, "total_cost_usd": 0.0, "avg_cost": 0.0,
        "name": name, "first_entry_date": date,
    }
    pos["shares"] = pos["shares"] + qty
    pos["total_cost_usd"] = pos["total_cost_usd"] + qty * price
    pos["avg_cost"] = pos["total_cost_usd"] / pos["shares"] if pos["shares"] else 0.0
    if name and not pos.get("name"):
        pos["name"] = name
    pos.setdefault("first_entry_date", date)
    positions[ticker] = pos
    return {**state, "positions": positions}


def apply_sell_fill(
    state: dict[str, Any], ticker: str, qty: int, price: float,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """SELL約定を反映した新しいstateと、建玉が無くなった場合の決着情報を返す。

    決着情報（closed）は「勝ち負けが確定した取引」の振り返りに使う。
    平均取得単価に対する実現損益で計算する（部分売却では建玉が残るためNoneを返す）。
    """
    positions = {t: dict(p) for t, p in state.get("positions", {}).items()}
    pos = positions.get(ticker)
    if pos is None:
        return state, None

    sell_qty = min(qty, pos["shares"])
    avg_cost = pos["avg_cost"]
    realized = (price - avg_cost) * sell_qty
    pos["shares"] = pos["shares"] - sell_qty
    pos["total_cost_usd"] = max(0.0, pos["total_cost_usd"] - avg_cost * sell_qty)
    pos["realized_pnl_usd"] = pos.get("realized_pnl_usd", 0.0) + realized

    closed: dict[str, Any] | None = None
    if pos["shares"] <= 0:
        closed = {
            "ticker": ticker,
            "name": pos.get("name"),
            "first_entry_date": pos.get("first_entry_date"),
            "avg_cost": round(avg_cost, 4),
            "exit_price": round(price, 4),
            "realized_pnl_usd": round(pos.get("realized_pnl_usd", 0.0), 2),
            "return_pct": round((price / avg_cost - 1) * 100, 2) if avg_cost else 0.0,
        }
        positions.pop(ticker, None)
    else:
        positions[ticker] = pos
    return {**state, "positions": positions}, closed


# ---------------------------------------------------------------------------
# ガードレール（AIが何を言おうとここで止める）
# ---------------------------------------------------------------------------

def validate_orders(
    orders: list[dict[str, Any]],
    state: dict[str, Any],
    prices: dict[str, float],
    tradable_tickers: set[str] | None,
    trade_date: str,
    max_orders_per_day: int = config.AI_MAX_ORDERS_PER_DAY,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """執行役が出した注文をガードレールに通す。純粋関数（発注はしない）。

    弾く条件（charter.md・指示書のガードレール）:
      - 未知のaction / 対象外の銘柄（米国上場・売買可能な普通株でないもの）
      - **自分の台帳に無い銘柄の売り**（他の枠の玉・口座全体の玉には触れない）
      - **保有株数を超える売り**（＝空売りの禁止）
      - **使える現金を超える買い**（＝レバレッジの禁止）
      - 整数株にならない金額（1株も買えない・売れない）
      - 理由が空・短すぎる注文
      - 1日の発注件数上限を超えた分

    現金・保有株数は注文を1件ずつ適用しながら見ていく（合計で超過しないため）。
    戻り値: (通した注文[qty付き], 弾いた注文[reject_reason付き])
    """
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    remaining_cash = compute_available_cash(state, prices)
    remaining_shares = {t: p["shares"] for t, p in open_positions(state).items()}
    for ticker in list(remaining_shares):
        remaining_shares[ticker] -= reserved_sell_shares(state, ticker)

    budget = max_orders_per_day - orders_placed_today(state, trade_date)

    def reject(order: dict[str, Any], why: str) -> None:
        rejected.append({**order, "reject_reason": why})
        logger.warning("AI裁量枠: 注文を拒否した（%s）: %s", why, order)

    for order in orders:
        action = str(order.get("action", "")).upper()
        ticker = str(order.get("ticker", "")).upper()
        reason = str(order.get("reason") or "").strip()

        if action not in ("BUY", "SELL"):
            reject(order, f"未知のaction: {action!r}")
            continue
        if not ticker:
            reject(order, "tickerが空")
            continue
        if len(reason) < config.AI_MIN_REASON_CHARS:
            reject(order, f"理由が短すぎる（{len(reason)}文字・最低{config.AI_MIN_REASON_CHARS}文字）")
            continue
        if len(accepted) >= budget:
            reject(order, f"1日の発注上限{max_orders_per_day}件に達した")
            continue

        price = prices.get(ticker)
        if price is None or price <= 0:
            reject(order, "現在値が取得できない（売買可能な銘柄か確認できない）")
            continue

        if action == "BUY":
            if tradable_tickers is not None and ticker not in tradable_tickers:
                reject(order, "米国の主要取引所に上場する普通株ではない（対象外）")
                continue
            try:
                amount = float(order.get("amount_usd") or 0.0)
            except (TypeError, ValueError):
                reject(order, "amount_usdが数値でない")
                continue
            if amount <= 0:
                reject(order, "amount_usdが0以下")
                continue
            qty = int(math.floor(amount / price))
            if qty < 1:
                reject(order, f"金額${amount:,.2f}では1株も買えない（現在値${price:,.2f}）")
                continue
            cost = qty * price
            if cost > remaining_cash + 1e-6:
                reject(order, f"現金不足（必要${cost:,.2f} / 使える現金${remaining_cash:,.2f}）")
                continue
            remaining_cash -= cost
            accepted.append({**order, "action": "BUY", "ticker": ticker, "qty": qty, "est_price": price})
            continue

        # SELL: 自分の台帳に載っている玉だけを、保有株数の範囲で売る
        held = remaining_shares.get(ticker, 0.0)
        if held <= 0:
            reject(order, "この枠の台帳に保有が無い（他の枠の玉・口座の玉は売れない）")
            continue
        try:
            fraction = float(order.get("sell_fraction") if order.get("sell_fraction") is not None else 1.0)
        except (TypeError, ValueError):
            reject(order, "sell_fractionが数値でない")
            continue
        if not (0 < fraction <= 1):
            reject(order, f"sell_fractionが範囲外（{fraction}）")
            continue
        qty = int(math.floor(held * fraction))
        if fraction >= 1.0:
            qty = int(held)
        if qty < 1:
            reject(order, f"売却株数が1株未満（保有{held}株 × {fraction}）")
            continue
        if qty > held:
            reject(order, f"保有株数を超える売り（{qty} > {held}）")
            continue
        remaining_shares[ticker] = held - qty
        accepted.append({**order, "action": "SELL", "ticker": ticker, "qty": qty, "est_price": price})

    return accepted, rejected
