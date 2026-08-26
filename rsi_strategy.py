"""vs-sp500: RSI枠のロット状態機械（SPEC_RSI30.md準拠。米国RSI-32枠と日本株RSI枠が共用する）。

本体（portfolio.py）とは完全に独立した戦略枠。同一銘柄に複数ロットが
並びうる（再エントリーにクールダウンは無い）。

この関数群は純粋関数のみで構成する（broker呼び出し・ファイルI/Oを含まない）。
「決定（decide_*）」と「反映（apply_*）」を分離しているのは、実際の発注では
見積り価格と実約定価格が異なりうるため（本体のexecute_tradesと同じ設計）。
テストは decide → apply を約定価格=見積り価格として直接呼び出すことで、
brokerに触れずにルールだけを検証する。

売買ルールの金額・閾値は StrategyRules にまとめ、引数で受け取る（2026-08-24改修。
日本株RSI枠の追加にあたり、ルールを複製せずこのモジュールを共用するため）。
省略時は US_RULES（従来のRSI-32枠の定数）を使うため、rulesを渡さない既存呼び出し・
既存テストの挙動は一切変わらない。

ロットのスキーマ:
    lot_id, ticker, name（企業名。取得できない場合はNone）, initial_entry_date, initial_entry_price,
    pyramid_done (3要素のbool配列: +2.5%/+5.0%/+7.5%),
    shares, total_invested_usd, avg_cost, lot_size（単元株数。米国は1固定）,
    profit1_taken, profit2_taken, base_shares（利確1発動時点の株数）,
    exception_active, exception_deadline_date,
    closed, closed_reason, closed_date
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import config


@dataclass(frozen=True)
class StrategyRules:
    """RSI枠の売買ルール定数。通貨は問わない（呼び出し側が金額の単位を揃える）。"""

    entry_rsi_threshold: float
    entry_amount: float
    pyramid_triggers: tuple[float, ...]
    pyramid_amounts: tuple[float, ...]
    profit1_pct: float
    profit1_sell_fraction: float
    profit2_pct: float
    profit2_sell_fraction: float
    exception_window_trading_days: int
    exception_hold_calendar_days: int


# RSI-32枠（米国・ドル建て）。SPEC_RSI30.md準拠、従来のconfig定数をそのまま束ねただけ。
US_RULES = StrategyRules(
    entry_rsi_threshold=config.RSI_ENTRY_RSI_THRESHOLD,
    entry_amount=config.RSI_ENTRY_AMOUNT_USD,
    pyramid_triggers=config.RSI_PYRAMID_TRIGGERS,
    pyramid_amounts=config.RSI_PYRAMID_AMOUNTS_USD,
    profit1_pct=config.RSI_PROFIT1_PCT,
    profit1_sell_fraction=config.RSI_PROFIT1_SELL_FRACTION,
    profit2_pct=config.RSI_PROFIT2_PCT,
    profit2_sell_fraction=config.RSI_PROFIT2_SELL_FRACTION,
    exception_window_trading_days=config.RSI_EXCEPTION_WINDOW_TRADING_DAYS,
    exception_hold_calendar_days=config.RSI_EXCEPTION_HOLD_CALENDAR_DAYS,
)

# 日本株RSI枠（円建て）。SPEC出典は2026-08-24 大将の発言（GOAL.mdおよびdaily_run.py組み込み指示書参照）。
JP_RULES = StrategyRules(
    entry_rsi_threshold=config.RSI_JP_ENTRY_RSI_THRESHOLD,
    entry_amount=config.RSI_JP_ENTRY_AMOUNT_JPY,
    pyramid_triggers=config.RSI_JP_PYRAMID_TRIGGERS,
    pyramid_amounts=config.RSI_JP_PYRAMID_AMOUNTS_JPY,
    profit1_pct=config.RSI_JP_PROFIT1_PCT,
    profit1_sell_fraction=config.RSI_JP_PROFIT1_SELL_FRACTION,
    profit2_pct=config.RSI_JP_PROFIT2_PCT,
    profit2_sell_fraction=config.RSI_JP_PROFIT2_SELL_FRACTION,
    exception_window_trading_days=config.RSI_JP_EXCEPTION_WINDOW_TRADING_DAYS,
    exception_hold_calendar_days=config.RSI_JP_EXCEPTION_HOLD_CALENDAR_DAYS,
)


def qty_for_amount(amount: float, price: float, lot_size: int = 1) -> int:
    """amount分をpriceで買える株数を、lot_size単位に切り捨てて返す。

    米国枠はlot_size=1（従来どおり1株単位の切り捨て）。日本株枠は単元株数を渡すことで
    「単元株数の倍数に切り捨て」（SPEC）を満たす。price<=0やlot_size<=0では0を返す。
    """
    if price <= 0 or lot_size <= 0:
        return 0
    raw_qty = int(amount / price + 1e-9)
    return (raw_qty // lot_size) * lot_size


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


def should_enter(rsi14: float, rules: StrategyRules = US_RULES) -> bool:
    """ENTRY: RSI(14) <= entry_rsi_threshold。"""
    return rsi14 <= rules.entry_rsi_threshold


def select_entries_within_cash(
    candidates: list[dict[str, Any]], available_cash: float, rules: StrategyRules = US_RULES,
) -> list[dict[str, Any]]:
    """新規エントリー候補をRSIが低い順に並べ、現金が足りる分だけ選ぶ。

    呼び出し側は買い増し（pyramid）を先に実行し終えた後の残現金をavailable_cashとして渡すこと
    （SPEC_RSI30.md「資金不足時: 買い増しを優先し、その後に新規エントリーをRSIが低い順に」）。
    現金が足りない候補はスキップし、次（RSIが次に低い候補）を試す。

    candidates: [{"ticker": str, "rsi14": float, "price": float, "lot_size": int(省略可・既定1)}, ...]
    lot_sizeを省略した候補（米国枠）は1株単位のまま従来どおり計算する。
    戻り値: 選ばれた候補に "qty" を付加したリスト（RSI昇順）。
    """
    remaining = available_cash
    selected: list[dict[str, Any]] = []
    for c in sorted(candidates, key=lambda x: x["rsi14"]):
        qty = qty_for_amount(rules.entry_amount, c["price"], c.get("lot_size", 1))
        if qty <= 0:
            continue
        cost = qty * c["price"]
        if cost > remaining + 1e-9:
            continue
        selected.append({**c, "qty": qty})
        remaining -= cost
    return selected


def filter_blocked_entries(
    candidates: list[dict[str, Any]], lots: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """新規エントリー候補から、対象銘柄に未クローズかつ利確前のロットがある分を除外する
    （2026-08-19改修1。大将「１だな」＝同じ銘柄は保有中は1ロットまで）。

    未クローズのロットが利確1（または利確2）を実施済み（＝「伸ばす玉」の状態）の銘柄は
    再エントリーを許可する。未クローズのロットが存在しない銘柄（全売却済み・未保有）も許可する。
    買い増し・利確の判定には一切関与しない。

    candidates: [{"ticker": str, ...}, ...]（select_entries_within_cashと同じ形）
    戻り値: (抑止後の候補リスト, 抑止された銘柄のリスト)
    """
    blocked_tickers = {
        lot["ticker"] for lot in lots
        if not lot["closed"] and not lot["profit1_taken"]
    }
    allowed = [c for c in candidates if c["ticker"] not in blocked_tickers]
    blocked = [c["ticker"] for c in candidates if c["ticker"] in blocked_tickers]
    return allowed, blocked


def new_lot(
    ticker: str, lot_id: str, entry_date: str, filled_qty: int, fill_price: float, lot_size: int = 1,
    name: str | None = None,
) -> dict[str, Any]:
    """RSIエントリー条件成立後の約定を受け、新しいロットを作る。

    lot_size: このロットの買い増しに使う単元株数（米国枠は既定の1のまま）。
    name: moomooスクリーナーから取得した企業名（2026-08-26追加。管理画面・Telegram報告で
    ティッカーだけでは判別できない銘柄を人間が識別できるようにするため。取得できなければNone）。
    フィールド名は"total_invested_usd"のままだが、日本株RSI枠では円建ての値を保持する
    （米国枠と完全に同じスキーマを共用するための既知の命名の名残。値の単位は通貨で変わる）。
    """
    total_invested = filled_qty * fill_price
    return {
        "lot_id": lot_id,
        "ticker": ticker,
        "name": name,
        "initial_entry_date": entry_date,
        "initial_entry_price": fill_price,
        "pyramid_done": [False, False, False],
        "shares": filled_qty,
        "lot_size": lot_size,
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


# ---------------------------------------------------------------------------
# 決定（decide_*）: 現在の状態と当日の価格から必要な意思決定を返す。状態は変更しない。
# ---------------------------------------------------------------------------

def decide_pyramid_buys(
    lot: dict[str, Any], price: float, rules: StrategyRules = US_RULES,
) -> list[dict[str, Any]]:
    """PYRAMID: 未実施の買い増し段のうち、価格が閾値(初期エントリー価格基準)を超えている分を全て返す。

    1日で複数段の閾値に同時到達した場合（大きな寄り付き）は複数件返す。
    キー名は"amount_usd"のままだが、日本株RSI枠では円建ての金額を保持する
    （new_lotのtotal_invested_usdと同じ命名の名残。intentは呼び出し元がすぐ消費する内部値）。
    """
    if lot["closed"]:
        return []
    intents = []
    base = lot["initial_entry_price"]
    for i, (trigger_pct, amount) in enumerate(zip(rules.pyramid_triggers, rules.pyramid_amounts)):
        if lot["pyramid_done"][i]:
            continue
        if price >= base * (1 + trigger_pct):
            intents.append({
                "kind": f"pyramid{i + 1}", "action": "BUY", "ticker": lot["ticker"], "lot_id": lot["lot_id"],
                "amount_usd": amount, "stage_index": i,
            })
    return intents


def decide_profit_takes(
    lot: dict[str, Any], price: float, current_date: str, trading_days_elapsed: int,
    rules: StrategyRules = US_RULES,
) -> list[dict[str, Any]]:
    """NORMAL PROFIT / EXCEPTION: 利確1(50%)・利確2(25%)の判定。

    例外発動中（exception_active かつ deadline未到達）は何も返さない。
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
        threshold = avg_cost * (1 + rules.profit1_pct)
        if price < threshold:
            return []
        if not lot["exception_active"] and trading_days_elapsed <= rules.exception_window_trading_days:
            return [{
                "kind": "exception_trigger", "action": "HOLD", "ticker": lot["ticker"], "lot_id": lot["lot_id"],
            }]
        qty = int(lot["shares"] * rules.profit1_sell_fraction)
        if qty <= 0:
            return []
        return [{
            "kind": "profit1", "action": "SELL", "ticker": lot["ticker"], "lot_id": lot["lot_id"],
            "qty": qty, "base_shares": lot["shares"],
        }]

    if not lot["profit2_taken"]:
        threshold = avg_cost * (1 + rules.profit2_pct)
        if price < threshold:
            return []
        base_shares = lot["base_shares"]
        qty = min(int(base_shares * rules.profit2_sell_fraction), int(lot["shares"]))
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


def apply_exception_trigger(lot: dict[str, Any], rules: StrategyRules = US_RULES) -> dict[str, Any]:
    new_lot = dict(lot)
    new_lot["exception_active"] = True
    entry = dt.date.fromisoformat(lot["initial_entry_date"])
    deadline = entry + dt.timedelta(days=rules.exception_hold_calendar_days)
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


# ---------------------------------------------------------------------------
# テスト・単一ロット検証用のオーケストレーション。
# 「その日ありうる決定を全て適用し終える」まで decide→apply を繰り返す
# （例: 寄り付きで+2.5%と+5.0%を同時に超える、利確1約定直後に利確2条件も満たす、等）。
# 実運用（rsi_daily.py）はbroker実約定を挟むため、この関数はそのまま流用しない。
# ---------------------------------------------------------------------------

def simulate_lot_day(
    lot: dict[str, Any], price: float, current_date: str, rules: StrategyRules = US_RULES,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """1ロット・1日分の判定と約定を、約定価格=price として全て適用する（テスト専用の簡易実行器）。

    戻り値: (更新後のlot, 発生した取引記録のリスト)
    """
    trades: list[dict[str, Any]] = []

    trading_days_elapsed = business_days_since(lot["initial_entry_date"], current_date)

    # 買い増し（同日に複数段到達しうる）。lot_sizeが無い（米国枠）ロットは1株単位のまま。
    for intent in decide_pyramid_buys(lot, price, rules):
        qty = qty_for_amount(intent["amount_usd"], price, lot.get("lot_size", 1))
        if qty <= 0:
            continue
        lot = apply_pyramid_fill(lot, intent["stage_index"], qty, price)
        trades.append({**intent, "fill_price": price, "filled_qty": qty})

    # 利確（利確1約定後に利確2条件も満たしうるため、決定が尽きるまで繰り返す）
    while True:
        intents = decide_profit_takes(lot, price, current_date, trading_days_elapsed, rules)
        if not intents:
            break
        intent = intents[0]
        if intent["kind"] == "exception_trigger":
            lot = apply_exception_trigger(lot, rules)
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
