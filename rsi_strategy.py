"""vs-sp500: RSI-30枠のロット状態機械（SPEC_RSI30.md準拠）。

本体（portfolio.py）とは完全に独立した2本目の戦略枠。同一銘柄に複数ロットが
並びうる（再エントリーにクールダウンは無い）。

この関数群は純粋関数のみで構成する（broker呼び出し・ファイルI/Oを含まない）。
「決定（decide_*）」と「反映（apply_*）」を分離しているのは、実際の発注では
見積り価格と実約定価格が異なりうるため（本体のexecute_tradesと同じ設計）。
テストは decide → apply を約定価格=見積り価格として直接呼び出すことで、
brokerに触れずにルールだけを検証する。

ロットのスキーマ:
    lot_id, ticker, initial_entry_date, initial_entry_price,
    pyramid_done (3要素のbool配列: +2.5%/+5.0%/+7.5%),
    shares, total_invested_usd, avg_cost,
    profit1_taken, profit2_taken, base_shares（利確1発動時点の株数）,
    exception_active, exception_deadline_date,
    closed, closed_reason, closed_date
"""
from __future__ import annotations

import datetime as dt
from typing import Any

import config


def business_days_since(start_date: str, current_date: str) -> int:
    """startからcurrentまでの営業日数（土日を除く単純カウント。祝日は考慮しない）。

    daily_run.pyの_business_days_betweenと同じ規約（本体との一貫性のため）。
    startが当日なら0を返す。
    """
    start = dt.date.fromisoformat(start_date)
    current = dt.date.fromisoformat(current_date)
    if current <= start:
        return 0
    count = 0
    d = start
    while d < current:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            count += 1
    return count


def should_enter(rsi14: float) -> bool:
    """ENTRY: RSI(14) <= 30。"""
    return rsi14 <= config.RSI_ENTRY_RSI_THRESHOLD


def select_entries_within_cash(candidates: list[dict[str, Any]], available_cash: float) -> list[dict[str, Any]]:
    """新規エントリー候補をRSIが低い順に並べ、現金が足りる分だけ選ぶ。

    呼び出し側は買い増し（pyramid）を先に実行し終えた後の残現金をavailable_cashとして渡すこと
    （SPEC_RSI30.md「資金不足時: 買い増しを優先し、その後に新規エントリーをRSIが低い順に」）。
    現金が足りない候補はスキップし、次（RSIが次に低い候補）を試す。

    candidates: [{"ticker": str, "rsi14": float, "price": float}, ...]
    戻り値: 選ばれた候補に "qty" を付加したリスト（RSI昇順）。
    """
    remaining = available_cash
    selected: list[dict[str, Any]] = []
    for c in sorted(candidates, key=lambda x: x["rsi14"]):
        qty = int(config.RSI_ENTRY_AMOUNT_USD / c["price"] + 1e-9)
        if qty <= 0:
            continue
        cost = qty * c["price"]
        if cost > remaining + 1e-9:
            continue
        selected.append({**c, "qty": qty})
        remaining -= cost
    return selected


def new_lot(ticker: str, lot_id: str, entry_date: str, filled_qty: int, fill_price: float) -> dict[str, Any]:
    """RSI<=30のエントリー約定後、新しいロットを作る。"""
    total_invested = filled_qty * fill_price
    return {
        "lot_id": lot_id,
        "ticker": ticker,
        "initial_entry_date": entry_date,
        "initial_entry_price": fill_price,
        "pyramid_done": [False, False, False],
        "shares": filled_qty,
        "total_invested_usd": total_invested,
        "avg_cost": fill_price,
        "profit1_taken": False,
        "profit2_taken": False,
        "base_shares": None,
        "exception_active": False,
        "exception_deadline_date": None,
        "closed": False,
        "closed_reason": None,
        "closed_date": None,
    }


def _stop_loss_price(lot: dict[str, Any]) -> float:
    return lot["initial_entry_price"] * (1 + config.RSI_STOP_LOSS_PCT)


def _is_runner_only(lot: dict[str, Any]) -> bool:
    """利確1・2が両方済みで、伸ばす玉（残り25%）だけが残っている状態か。"""
    return bool(lot["profit1_taken"] and lot["profit2_taken"])


# ---------------------------------------------------------------------------
# 決定（decide_*）: 現在の状態と当日の価格から必要な意思決定を返す。状態は変更しない。
# ---------------------------------------------------------------------------

def decide_stop_loss(lot: dict[str, Any], price: float) -> dict[str, Any] | None:
    """STOP: 株価が初期エントリー価格×0.92以下 → 全株売却。

    例外（8週ホールド）中も有効。ただし伸ばす玉（利確1・2済み）のみを保有していて
    かつ含み益が出ている場合は対象外（SPEC_RSI30.md「伸ばす玉」「利益が出ている場合は適用しない」）。
    """
    if lot["closed"] or lot["shares"] <= 0:
        return None
    if price > _stop_loss_price(lot):
        return None
    if _is_runner_only(lot) and price > lot["avg_cost"]:
        return None
    return {
        "kind": "stop_loss", "action": "SELL", "ticker": lot["ticker"], "lot_id": lot["lot_id"],
        "qty": int(lot["shares"]),
    }


def decide_pyramid_buys(lot: dict[str, Any], price: float) -> list[dict[str, Any]]:
    """PYRAMID: 未実施の買い増し段のうち、価格が閾値(初期エントリー価格基準)を超えている分を全て返す。

    1日で複数段の閾値に同時到達した場合（大きな寄り付き）は複数件返す。
    """
    if lot["closed"]:
        return []
    intents = []
    base = lot["initial_entry_price"]
    for i, (trigger_pct, amount) in enumerate(zip(config.RSI_PYRAMID_TRIGGERS, config.RSI_PYRAMID_AMOUNTS_USD)):
        if lot["pyramid_done"][i]:
            continue
        if price >= base * (1 + trigger_pct):
            intents.append({
                "kind": f"pyramid{i + 1}", "action": "BUY", "ticker": lot["ticker"], "lot_id": lot["lot_id"],
                "amount_usd": amount, "stage_index": i,
            })
    return intents


def decide_profit_takes(lot: dict[str, Any], price: float, current_date: str, trading_days_elapsed: int) -> list[dict[str, Any]]:
    """NORMAL PROFIT / EXCEPTION: 利確1(50%)・利確2(25%)の判定。

    例外発動中（exception_active かつ deadline未到達）は何も返さない（損切りは別関数で有効）。
    利確1が未実施の状態で+20%条件が満たされた日が初期エントリーから15営業日以内なら、
    売らずに例外を発動する意図（kind="exception_trigger"）だけを返す。
    利確1が未実施のうちは利確2を判定しない（base_sharesが確定していないため）。
    """
    if lot["closed"] or lot["shares"] <= 0:
        return []
    if lot["exception_active"] and lot["exception_deadline_date"] and current_date < lot["exception_deadline_date"]:
        return []

    avg_cost = lot["avg_cost"]

    if not lot["profit1_taken"]:
        threshold = avg_cost * (1 + config.RSI_PROFIT1_PCT)
        if price < threshold:
            return []
        if not lot["exception_active"] and trading_days_elapsed <= config.RSI_EXCEPTION_WINDOW_TRADING_DAYS:
            return [{
                "kind": "exception_trigger", "action": "HOLD", "ticker": lot["ticker"], "lot_id": lot["lot_id"],
            }]
        qty = int(lot["shares"] * config.RSI_PROFIT1_SELL_FRACTION)
        if qty <= 0:
            return []
        return [{
            "kind": "profit1", "action": "SELL", "ticker": lot["ticker"], "lot_id": lot["lot_id"],
            "qty": qty, "base_shares": lot["shares"],
        }]

    if not lot["profit2_taken"]:
        threshold = avg_cost * (1 + config.RSI_PROFIT2_PCT)
        if price < threshold:
            return []
        base_shares = lot["base_shares"]
        qty = min(int(base_shares * config.RSI_PROFIT2_SELL_FRACTION), int(lot["shares"]))
        if qty <= 0:
            return []
        return [{
            "kind": "profit2", "action": "SELL", "ticker": lot["ticker"], "lot_id": lot["lot_id"],
            "qty": qty,
        }]

    return []


# ---------------------------------------------------------------------------
# 反映（apply_*）: 実約定結果（filled_qty・fill_price）をロットへ反映した新しいロットを返す。
# ---------------------------------------------------------------------------

def apply_pyramid_fill(lot: dict[str, Any], stage_index: int, filled_qty: int, fill_price: float) -> dict[str, Any]:
    new_lot = dict(lot)
    new_lot["pyramid_done"] = list(lot["pyramid_done"])
    new_lot["pyramid_done"][stage_index] = True
    new_shares = lot["shares"] + filled_qty
    new_invested = lot["total_invested_usd"] + filled_qty * fill_price
    new_lot["shares"] = new_shares
    new_lot["total_invested_usd"] = new_invested
    new_lot["avg_cost"] = new_invested / new_shares if new_shares > 0 else lot["avg_cost"]
    return new_lot


def apply_exception_trigger(lot: dict[str, Any]) -> dict[str, Any]:
    new_lot = dict(lot)
    new_lot["exception_active"] = True
    entry = dt.date.fromisoformat(lot["initial_entry_date"])
    deadline = entry + dt.timedelta(days=config.RSI_EXCEPTION_HOLD_CALENDAR_DAYS)
    new_lot["exception_deadline_date"] = deadline.isoformat()
    return new_lot


def apply_profit1_fill(lot: dict[str, Any], filled_qty: int, base_shares: int) -> dict[str, Any]:
    new_lot = dict(lot)
    new_lot["shares"] = lot["shares"] - filled_qty
    new_lot["profit1_taken"] = True
    new_lot["base_shares"] = base_shares
    return new_lot


def apply_profit2_fill(lot: dict[str, Any], filled_qty: int) -> dict[str, Any]:
    new_lot = dict(lot)
    new_lot["shares"] = lot["shares"] - filled_qty
    new_lot["profit2_taken"] = True
    return new_lot


def apply_stop_loss_fill(lot: dict[str, Any], filled_qty: int, current_date: str) -> dict[str, Any]:
    new_lot = dict(lot)
    new_lot["shares"] = lot["shares"] - filled_qty
    if new_lot["shares"] <= 0:
        new_lot["closed"] = True
        new_lot["closed_reason"] = "stop_loss"
        new_lot["closed_date"] = current_date
    return new_lot


# ---------------------------------------------------------------------------
# テスト・単一ロット検証用のオーケストレーション。
# 「その日ありうる決定を全て適用し終える」まで decide→apply を繰り返す
# （例: 寄り付きで+2.5%と+5.0%を同時に超える、利確1約定直後に利確2条件も満たす、等）。
# 実運用（rsi_daily.py）はbroker実約定を挟むため、この関数はそのまま流用しない。
# ---------------------------------------------------------------------------

def simulate_lot_day(lot: dict[str, Any], price: float, current_date: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """1ロット・1日分の判定と約定を、約定価格=price として全て適用する（テスト専用の簡易実行器）。

    戻り値: (更新後のlot, 発生した取引記録のリスト)
    """
    trades: list[dict[str, Any]] = []

    stop = decide_stop_loss(lot, price)
    if stop is not None:
        lot = apply_stop_loss_fill(lot, stop["qty"], current_date)
        trades.append({**stop, "fill_price": price, "filled_qty": stop["qty"]})
        return lot, trades  # 損切りが出た日は他の判定を行わない

    trading_days_elapsed = business_days_since(lot["initial_entry_date"], current_date)

    # 買い増し（同日に複数段到達しうる）
    for intent in decide_pyramid_buys(lot, price):
        qty = int(intent["amount_usd"] / price + 1e-9)
        if qty <= 0:
            continue
        lot = apply_pyramid_fill(lot, intent["stage_index"], qty, price)
        trades.append({**intent, "fill_price": price, "filled_qty": qty})

    # 利確（利確1約定後に利確2条件も満たしうるため、決定が尽きるまで繰り返す）
    while True:
        intents = decide_profit_takes(lot, price, current_date, trading_days_elapsed)
        if not intents:
            break
        intent = intents[0]
        if intent["kind"] == "exception_trigger":
            lot = apply_exception_trigger(lot)
            trades.append(intent)
            break
        if intent["kind"] == "profit1":
            lot = apply_profit1_fill(lot, intent["qty"], intent["base_shares"])
            trades.append({**intent, "fill_price": price, "filled_qty": intent["qty"]})
            continue
        if intent["kind"] == "profit2":
            lot = apply_profit2_fill(lot, intent["qty"])
            trades.append({**intent, "fill_price": price, "filled_qty": intent["qty"]})
            continue
        break

    return lot, trades
