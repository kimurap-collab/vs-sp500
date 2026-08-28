"""vs-sp500: AI裁量枠の毎日実行ロジック（4本目の戦略枠。2026-08-28新設）。

daily_run.py が本体・米国RSI枠・日本株RSI枠を終えた後に run() を1回呼ぶ。
既存3枠の台帳・判断・moomoo呼び出しには一切触れない（broker.pyは共用するが、
place_market_orderは常に TrdEnv.SIMULATE / config.MOOMOO_ACC_ID に固定されており、
このファイルが増えても実口座への発注経路は1本も増えない。unlock_tradeは呼ばない）。

1日の流れ:
  未決注文の決済 → 探索役A(条件) → スクリーナー → 探索役B(候補) → 検証役(潰す)
  → 執行役(発注量と理由) → ガードレール → 発注 → 台帳・理由・振り返りの記録
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import ai_agents
import ai_ledger
import broker
import config
from market import TickerSnapshot

logger = logging.getLogger("vs-sp500.ai_daily")


# ---------------------------------------------------------------------------
# 価格取得
# ---------------------------------------------------------------------------

def fetch_prices(tickers: list[str]) -> dict[str, dict[str, Any]]:
    """指定銘柄の直近値・日付をmoomooから一括取得する（rsi_daily.fetch_market_dataと同じ方式）。"""
    if not tickers:
        return {}
    snapshot = broker.get_snapshot(tickers)
    if snapshot is None:
        logger.error("AI裁量枠: 価格取得(get_market_snapshot)に失敗した")
        return {}
    today = dt.date.today().isoformat()
    return {t: {"close": price, "date": today} for t, price in snapshot.items()}


def compute_snapshot_only(
    ai_state: dict[str, Any], voo_snap: TickerSnapshot,
) -> tuple[float, float, dict[str, TickerSnapshot]]:
    """保有銘柄の価格だけを取ってNAV/ベンチマークを計算する（探索・売買は行わない）。

    --report-only・異常停止時など、表示更新のみが必要な場面で使う。
    """
    held = sorted(ai_ledger.open_positions(ai_state))
    prices = fetch_prices(held) if held else {}
    market = {
        t: TickerSnapshot(ticker=t, close=info["close"], date=info["date"])
        for t, info in prices.items()
    }
    nav_usd = ai_ledger.compute_nav_usd(ai_state, market)
    bench_usd = ai_ledger.compute_bench_nav_usd(ai_state, voo_snap.close) if ai_state.get("bench_units_ai") else 0.0
    return nav_usd, bench_usd, market


# ---------------------------------------------------------------------------
# 未決注文の決済
# ---------------------------------------------------------------------------

def settle_pending_orders(
    ai_state: dict[str, Any], today: str, dry_run: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str], list[str]]:
    """AI裁量枠のpending_ordersをmoomooに問い合わせ、確定した分を玉・現金へ反映する。

    方針はRSI枠のsettle_pending_orders（rsi_daily.py）と同じ。反映先がロットではなく
    positionsである点だけが異なる。**他の枠のpending_ordersには一切触れない。**

    戻り値: (新しいstate, 反映した取引ログ, 警告メッセージ, 自己解決の記録)
    """
    state = dict(ai_state)
    state["positions"] = {t: dict(p) for t, p in ai_state.get("positions", {}).items()}
    pending = list(ai_state.get("pending_orders", []))
    remaining: list[dict[str, Any]] = []
    applied_trades: list[dict[str, Any]] = []
    warnings: list[str] = []
    resolved_notes: list[str] = []
    market_open = broker.is_market_open_us() if pending else None

    for order in pending:
        info = broker.get_order_status(order["order_id"])
        if info is None:
            remaining.append(order)
            continue

        if info["status"] == "NOT_FOUND":
            resolved_notes.append(
                f"{order['ticker']}のAI裁量枠注文(order_id={order['order_id']})が見つからず"
                f"pendingから除外した（rule={order.get('rule')}）"
            )
            continue

        applied_qty = order.get("applied_qty", 0)
        applied_value = order.get("applied_value_usd", 0.0)
        dealt_qty = info["filled_qty"]
        status = info["status"]

        new_fill_qty = dealt_qty - applied_qty
        if new_fill_qty > 0:
            total_value = dealt_qty * info["avg_price"]
            incremental_value = total_value - applied_value
            incremental_price = incremental_value / new_fill_qty
            fill_date = info.get("updated_date") or order["submitted_date"]
            side = order["side"]
            ticker = order["ticker"]

            closed = None
            if side == "BUY":
                state = ai_ledger.apply_buy_fill(
                    state, ticker, new_fill_qty, incremental_price, fill_date, order.get("name"),
                )
                state["cash_usd"] -= incremental_price * new_fill_qty
            else:
                state, closed = ai_ledger.apply_sell_fill(state, ticker, new_fill_qty, incremental_price)
                state["cash_usd"] += incremental_price * new_fill_qty

            trade_row = {
                "date": fill_date, "action": side, "ticker": ticker, "name": order.get("name"),
                "shares": new_fill_qty, "price": round(incremental_price, 4),
                "amount_usd": round(new_fill_qty * incremental_price, 2),
                "rule": order.get("rule", ""),
                "reason": order.get("reason", ""),
                "note": f"pending決済(order_id={order['order_id']})・手数料不明のため未計上",
            }
            applied_trades.append(trade_row)
            if closed is not None and not dry_run:
                _record_review(closed, fill_date, order.get("reason", ""), order.get("review"))
            warnings.append(
                f"AI裁量枠pending決済: {ticker} {side} {new_fill_qty}株 @ {incremental_price:.4f}"
                f"（order_id={order['order_id']}・手数料は台帳に未計上）"
            )
            applied_qty = dealt_qty
            applied_value = total_value

        if status == "FILLED_ALL" or applied_qty >= order["qty"]:
            continue

        if status in broker.ORDER_TERMINAL_STATUSES:
            if applied_qty < order["qty"]:
                warnings.append(
                    f"AI裁量枠: 未決注文が未達のまま終端した（status={status}）: "
                    f"order_id={order['order_id']} {order['ticker']} 残数{order['qty'] - applied_qty}株は打ち切り"
                )
            continue

        if market_open is True:
            if dry_run:
                warnings.append(
                    f"[dry-run] 滞留注文のキャンセル対象（実行はスキップ）: order_id={order['order_id']} "
                    f"{order['ticker']}"
                )
                remaining.append({**order, "applied_qty": applied_qty, "applied_value_usd": applied_value})
                continue
            if broker.cancel_order(order["order_id"]):
                resolved_notes.append(
                    f"{order['ticker']}のAI裁量枠注文(order_id={order['order_id']})が場中に滞留したため"
                    f"キャンセルした（適用済み{applied_qty}/{order['qty']}株）"
                )
            else:
                warnings.append(
                    f"AI裁量枠: 滞留注文のキャンセル要求に失敗した: order_id={order['order_id']} {order['ticker']}"
                )
                remaining.append({**order, "applied_qty": applied_qty, "applied_value_usd": applied_value})
            continue

        remaining.append({**order, "applied_qty": applied_qty, "applied_value_usd": applied_value})

    state["pending_orders"] = remaining
    return state, applied_trades, warnings, resolved_notes


def _record_review(
    closed: dict[str, Any], date: str, exit_reason: str, executor_review: str | None,
) -> None:
    """建玉が無くなった＝勝ち負けが確定した取引の振り返りを残す。後から書き換えない。"""
    entry_reason = ""
    for row in ai_ledger.read_trade_rows():
        if row.get("ticker") == closed["ticker"] and row.get("action") == "BUY":
            entry_reason = row.get("reason", "")
    holding_days = ""
    try:
        if closed.get("first_entry_date"):
            holding_days = (dt.date.fromisoformat(date) - dt.date.fromisoformat(closed["first_entry_date"])).days
    except ValueError:
        pass
    ai_ledger.append_review({
        "date": date,
        "ticker": closed["ticker"],
        "name": closed.get("name"),
        "entry_date": closed.get("first_entry_date"),
        "holding_days": holding_days,
        "avg_cost": closed.get("avg_cost"),
        "exit_price": closed.get("exit_price"),
        "realized_pnl_usd": closed.get("realized_pnl_usd"),
        "return_pct": closed.get("return_pct"),
        "entry_reason": entry_reason,
        "exit_reason": exit_reason,
        "review": executor_review or "",
    })


# ---------------------------------------------------------------------------
# AIに渡す文脈（記憶）
# ---------------------------------------------------------------------------

def build_context(
    state: dict[str, Any],
    prices: dict[str, float],
    voo_snap: TickerSnapshot,
    voo_technicals: Any,
    nav_usd: float,
    bench_usd: float,
    trade_date: str,
) -> dict[str, Any]:
    """探索役・検証役・執行役に共通で渡す文脈を組む。

    **過去の判断とその結果を必ず入れる**（大将「自律改善するのも勝手だ」に対する学習の材料）。
    トークンを無駄に食わないよう、日誌は直近AI_MEMORY_DECISION_DAYS日、
    振り返りは直近AI_MEMORY_MAX_REVIEWS件に絞る。
    """
    positions = []
    for ticker, pos in ai_ledger.open_positions(state).items():
        price = prices.get(ticker)
        positions.append({
            "ticker": ticker,
            "name": pos.get("name"),
            "shares": pos["shares"],
            "avg_cost": round(pos["avg_cost"], 4),
            "price": round(price, 4) if price else None,
            "value_usd": round(pos["shares"] * price, 2) if price else None,
            "unrealized_pct": round((price / pos["avg_cost"] - 1) * 100, 2)
                              if price and pos["avg_cost"] else None,
            "first_entry_date": pos.get("first_entry_date"),
        })

    past_decisions = ai_ledger.read_decisions()[-config.AI_MEMORY_DECISION_DAYS:]
    slim_decisions = [
        {
            "date": d.get("date"),
            "screen_rationale": (d.get("screen_plan") or {}).get("rationale"),
            # 条件が厳しすぎて0件だった日を翌日の自分が見て直せるように必ず入れる
            "screen_filters": d.get("screen_plan"),
            "screen_rows": d.get("screen_rows"),
            "orders": [
                {"action": o.get("action"), "ticker": o.get("ticker"), "rule": o.get("rule"),
                 "reason": o.get("reason")}
                for o in (d.get("executed") or [])
            ],
            "no_action_reason": d.get("no_action_reason"),
            "killed": [v.get("ticker") for v in (d.get("challenger") or []) if v.get("verdict") == "kill"],
        }
        for d in past_decisions
    ]

    return {
        "today": trade_date,
        "book": {
            "name": "AI裁量枠",
            "start_date": state.get("start_date"),
            "initial_capital_usd": config.AI_INITIAL_CAPITAL_USD,
            "cash_usd": round(state["cash_usd"], 2),
            "available_cash_usd": round(ai_ledger.compute_available_cash(state, prices), 2),
            "nav_usd": round(nav_usd, 2),
            "benchmark_voo_usd": round(bench_usd, 2),
            "diff_vs_benchmark_usd": round(nav_usd - bench_usd, 2),
            "positions": positions,
            "pending_orders": state.get("pending_orders", []),
            "orders_already_placed_today": ai_ledger.orders_placed_today(state, trade_date),
        },
        "market": {
            "voo_close": voo_snap.close,
            "voo_date": voo_snap.date,
            "voo_ma200": getattr(voo_technicals, "ma200", None),
            "voo_rsi14": getattr(voo_technicals, "rsi14", None),
            "voo_high_52w": getattr(voo_technicals, "high_52w", None),
        },
        "past_decisions": slim_decisions,
        "closed_trade_reviews": ai_ledger.read_reviews()[-config.AI_MEMORY_MAX_REVIEWS:],
    }


# ---------------------------------------------------------------------------
# 3役を走らせる
# ---------------------------------------------------------------------------

def deliberate(
    context: dict[str, Any], tradable: dict[str, str] | None,
) -> tuple[dict[str, Any], list[str]]:
    """探索役→検証役→執行役を順に走らせる。発注はしない（判断だけを返す）。

    戻り値: (判断の記録, ログ行)。記録は decisions.jsonl にそのまま入る形。
    """
    logs: list[str] = []
    usage_total = {"input_tokens": 0, "output_tokens": 0}
    record: dict[str, Any] = {
        "date": context["today"],
        "screen_plan": None, "screen_rows": 0,
        "scout": None, "challenger": None, "executor": None,
        "orders": [], "no_action_reason": "", "usage": usage_total,
    }

    def add_usage(u: dict[str, int]) -> None:
        usage_total["input_tokens"] += u.get("input_tokens", 0)
        usage_total["output_tokens"] += u.get("output_tokens", 0)

    # --- 探索役A: 今日の絞り込み条件 ---
    plan, usage = ai_agents.scout_plan(context)
    add_usage(usage)
    if plan is None:
        record["no_action_reason"] = "探索役A（絞り込み条件）の呼び出しに失敗したため、今日は何もしない"
        logs.append("[AI-1] 探索役A失敗 → 見送り")
        return record, logs
    record["screen_plan"] = plan
    logs.append(f"[AI-1] 探索役A: {plan.get('rationale', '')[:160]}")

    # --- スクリーナー ---
    screened = ai_agents.run_screener(plan, tradable)
    if screened is None:
        record["no_action_reason"] = "スクリーナーの実行に失敗したため、今日は何もしない"
        logs.append("[AI-2] スクリーナー失敗 → 見送り")
        return record, logs
    record["screen_rows"] = len(screened)
    logs.append(f"[AI-2] スクリーナー: {len(screened)}銘柄")

    # --- 探索役B: 候補 ---
    scout, usage = ai_agents.scout_pick(context, screened, plan)
    add_usage(usage)
    if scout is None:
        record["no_action_reason"] = "探索役B（候補選定）の呼び出しに失敗したため、今日は何もしない"
        logs.append("[AI-3] 探索役B失敗 → 見送り")
        return record, logs
    record["scout"] = scout
    buys = scout.get("buy_candidates", [])
    sells = scout.get("sell_candidates", [])
    logs.append(
        f"[AI-3] 探索役B: 買い候補{len(buys)}件({', '.join(c['ticker'] for c in buys) or 'なし'})"
        f" 売り候補{len(sells)}件({', '.join(c['ticker'] for c in sells) or 'なし'})"
    )

    if not buys and not sells:
        record["no_action_reason"] = (
            "探索役が候補を1件も挙げなかった。市況判断: " + str(scout.get("market_read", ""))
        )
        logs.append("[AI-4] 候補なし → 見送り")
        return record, logs

    # --- 検証役: 潰す ---
    challenger, usage = ai_agents.challenge(context, scout)
    add_usage(usage)
    if challenger is None:
        # 検証役が動かなかった日は買わない。既定は「買わない」（指示書の「反論できなければ買わない」）
        record["no_action_reason"] = "検証役の呼び出しに失敗したため、既定どおり何も買わない"
        logs.append("[AI-4] 検証役失敗 → 既定（買わない）")
        return record, logs
    verdicts = challenger.get("verdicts", [])
    record["challenger"] = verdicts

    survived_keys = {
        (str(v.get("ticker", "")).upper(), str(v.get("side", "")).upper())
        for v in verdicts if v.get("verdict") == "survive"
    }
    killed = [v for v in verdicts if v.get("verdict") != "survive"]
    survivors = {
        "buy": [c for c in buys if (str(c["ticker"]).upper(), "BUY") in survived_keys],
        "sell": [c for c in sells if (str(c["ticker"]).upper(), "SELL") in survived_keys],
        "killed": [{"ticker": v.get("ticker"), "side": v.get("side"), "attack": v.get("attack")} for v in killed],
        "challenger_overall": challenger.get("overall", ""),
    }
    logs.append(
        f"[AI-4] 検証役: 生存 買い{len(survivors['buy'])}件/売り{len(survivors['sell'])}件・"
        f"却下{len(killed)}件({', '.join(str(v.get('ticker')) for v in killed) or 'なし'})"
    )

    if not survivors["buy"] and not survivors["sell"]:
        record["no_action_reason"] = (
            "検証役が全ての候補を潰した。既定どおり買わない。総括: " + str(challenger.get("overall", ""))
        )
        logs.append("[AI-5] 全候補が却下 → 見送り")
        return record, logs

    # --- 執行役: 発注量と理由 ---
    executor, usage = ai_agents.execute_plan(context, survivors)
    add_usage(usage)
    if executor is None:
        record["no_action_reason"] = "執行役の呼び出しに失敗したため、今日は発注しない"
        logs.append("[AI-5] 執行役失敗 → 見送り")
        return record, logs
    record["executor"] = executor

    # 執行役が潰された候補を発注しようとしても、ここで落とす（検証役の判定を必ず通す）
    orders = []
    for order in executor.get("orders", []):
        key = (str(order.get("ticker", "")).upper(), str(order.get("action", "")).upper())
        if key not in survived_keys:
            logs.append(f"[AI-5] 執行役の注文を却下: {key} は検証役を通っていない")
            continue
        orders.append(order)
    record["orders"] = orders
    record["no_action_reason"] = executor.get("no_action_reason", "") if not orders else ""
    logs.append(f"[AI-5] 執行役: 注文{len(orders)}件")
    return record, logs


# ---------------------------------------------------------------------------
# 発注
# ---------------------------------------------------------------------------

def _place(ticker: str, qty: int, side: str, market_us: str | None) -> tuple[dict[str, Any] | None, float]:
    """broker経由で成行発注し、実約定と現金差分を返す（rsi_daily._execute_orderと同じ方式）。"""
    if market_us is not None and market_us != broker.MARKET_US_OPEN_STATE:
        logger.info("AI裁量枠 %s %s: 市場が開いていないため発注を見送った（market_us=%s）", side, ticker, market_us)
        return None, 0.0
    cash_before = broker.get_cash()
    fill = broker.place_market_order(ticker, qty, side)
    if fill is None:
        return None, 0.0
    cash_after = broker.get_cash()
    if cash_before is not None and cash_after is not None:
        cash_delta = cash_after - cash_before
    else:
        logger.warning("AI裁量枠 %s %s: moomoo現金取得に失敗したため約定額から見積もった", side, ticker)
        cash_delta = (-1 if side == "BUY" else 1) * fill["filled_qty"] * fill["avg_price"]
    return fill, cash_delta


def execute_orders(
    state: dict[str, Any],
    orders: list[dict[str, Any]],
    trade_date: str,
    market_us: str | None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """ガードレールを通った注文を実際に発注し、台帳・理由を記録する。"""
    accepted_trades: list[dict[str, Any]] = []
    logs: list[str] = []
    placed = ai_ledger.orders_placed_today(state, trade_date)

    for order in orders:
        ticker, qty, side = order["ticker"], order["qty"], order["action"]
        fill, cash_delta = _place(ticker, qty, side, market_us)
        if fill is None:
            logs.append(f"[AI-6] 発注できなかった: {side} {ticker} {qty}株")
            continue

        placed += 1
        state = {**state, "orders_placed_today": {"date": trade_date, "count": placed}}

        outcome = broker.classify_fill(qty, fill)
        filled_qty, avg_price = fill["filled_qty"], fill["avg_price"]
        if outcome == "NONE_TERMINAL":
            logs.append(f"[AI-6] 約定せず終端した: {side} {ticker} status={fill['status']}")
            continue

        if filled_qty > 0:
            state = {**state, "cash_usd": state["cash_usd"] + cash_delta}
            closed = None
            if side == "BUY":
                state = ai_ledger.apply_buy_fill(
                    state, ticker, filled_qty, avg_price, trade_date, order.get("name"),
                )
            else:
                state, closed = ai_ledger.apply_sell_fill(state, ticker, filled_qty, avg_price)

            trade_row = {
                "date": trade_date, "action": side, "ticker": ticker,
                "name": (state.get("positions", {}).get(ticker) or {}).get("name") or order.get("name"),
                "shares": filled_qty, "price": round(avg_price, 4),
                "amount_usd": round(filled_qty * avg_price, 2),
                "rule": order.get("rule", ""),
                "reason": order.get("reason", ""),
                "note": "",
            }
            ai_ledger.append_trade_row(trade_row)
            accepted_trades.append(trade_row)
            if closed is not None:
                _record_review(closed, trade_date, order.get("reason", ""), order.get("review"))

        if outcome in ("PARTIAL_OPEN", "NONE_OPEN"):
            state.setdefault("pending_orders", [])
            state["pending_orders"] = state["pending_orders"] + [{
                "order_id": fill["order_id"], "ticker": ticker, "side": side,
                "qty": qty, "submitted_date": trade_date,
                "applied_qty": filled_qty, "applied_value_usd": filled_qty * avg_price,
                "rule": order.get("rule", ""), "reason": order.get("reason", ""),
                "review": order.get("review"), "est_price": order.get("est_price"),
                "name": order.get("name"),
            }]
        elif outcome == "PARTIAL_TERMINAL":
            logs.append(f"[AI-6] 一部約定({filled_qty}/{qty}株)のまま終端した: {side} {ticker}")

    return state, accepted_trades, logs


# ---------------------------------------------------------------------------
# 1日の処理
# ---------------------------------------------------------------------------

def run(
    ai_state: dict[str, Any],
    voo_snap: TickerSnapshot,
    voo_technicals: Any,
    can_trade: bool,
    already_processed_today: bool,
    dry_run: bool,
    trade_date: str,
    market_us: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str], float, float, dict[str, TickerSnapshot]]:
    """AI裁量枠の1日分。未決注文の決済はdaily_run.py側で先に済ませてある。

    戻り値: (state, 約定した取引, ログ行, NAV, ベンチマーク評価額, 保有の市場スナップショット)
    """
    log_lines: list[str] = []
    accepted_trades: list[dict[str, Any]] = []
    state = dict(ai_state)
    state["positions"] = {t: dict(p) for t, p in ai_state.get("positions", {}).items()}

    if state.get("start_date") is None:
        state["start_date"] = voo_snap.date
        state["bench_units_ai"] = config.AI_INITIAL_CAPITAL_USD / voo_snap.close
        log_lines.append(
            f"[AI-0] 初回構築: start_date={state['start_date']} "
            f"bench_units_ai={state['bench_units_ai']:.6f}（VOOに${config.AI_INITIAL_CAPITAL_USD:,.0f}一括投資）"
        )

    held = sorted(ai_ledger.open_positions(state))
    held_prices = fetch_prices(held) if held else {}
    prices = {t: v["close"] for t, v in held_prices.items()}

    do_trade = can_trade and not already_processed_today and not dry_run
    decision_record: dict[str, Any] = {"date": trade_date}

    if do_trade:
        tradable = ai_agents.fetch_tradable_universe()
        nav_now = ai_ledger.compute_nav_usd(
            state, {t: TickerSnapshot(t, v["close"], v["date"]) for t, v in held_prices.items()},
        )
        bench_now = ai_ledger.compute_bench_nav_usd(state, voo_snap.close)
        context = build_context(state, prices, voo_snap, voo_technicals, nav_now, bench_now, trade_date)

        decision_record, deliberation_logs = deliberate(context, tradable)
        log_lines.extend(deliberation_logs)

        raw_orders = decision_record.get("orders", [])
        if raw_orders:
            # 発注対象の価格を取り直す（保有していない銘柄の現在値がまだ無いため）
            need = sorted({str(o.get("ticker", "")).upper() for o in raw_orders} - set(prices))
            if need:
                prices = {**prices, **{t: v["close"] for t, v in fetch_prices(need).items()}}
            valid, rejected = ai_ledger.validate_orders(
                raw_orders, state, prices, set(tradable) if tradable else None, trade_date,
            )
            for order in valid:
                order["name"] = (tradable or {}).get(order["ticker"])
            decision_record["rejected"] = rejected
            if rejected:
                log_lines.append(
                    "[AI-6] ガードレールで拒否: "
                    + "; ".join(f"{r.get('action')} {r.get('ticker')}→{r['reject_reason']}" for r in rejected)
                )
            state, accepted_trades, exec_logs = execute_orders(state, valid, trade_date, market_us)
            log_lines.extend(exec_logs)
            decision_record["executed"] = accepted_trades
            log_lines.append(f"[AI-7] 約定{len(accepted_trades)}件")
        else:
            decision_record["executed"] = []
            log_lines.append(f"[AI-7] 発注なし: {decision_record.get('no_action_reason', '')[:200]}")

        state["last_processed_date"] = trade_date
        ai_ledger.append_decision(decision_record)
    else:
        reason = "dry-run" if dry_run else ("休場/処理済み" if already_processed_today else "売買停止中")
        log_lines.append(f"[AI-1] 判断スキップ（{reason}）")

    market_snapshots = {
        t: TickerSnapshot(ticker=t, close=info["close"], date=info["date"])
        for t, info in held_prices.items()
    }
    nav_usd = ai_ledger.compute_nav_usd(state, market_snapshots)
    bench_usd = ai_ledger.compute_bench_nav_usd(state, voo_snap.close)
    diff_usd = nav_usd - bench_usd
    cash_ratio = ai_ledger.compute_cash_ratio(state, nav_usd) if nav_usd else 0.0
    log_lines.append(f"[AI-8] 評価額: NAV=${nav_usd:,.2f} ベンチマーク=${bench_usd:,.2f} 差額=${diff_usd:,.2f}")

    if not dry_run:
        ai_ledger.append_history_row({
            "date": voo_snap.date,
            "nav_usd": round(nav_usd, 2),
            "bench_usd": round(bench_usd, 2),
            "diff_usd": round(diff_usd, 2),
            "diff_pct": round(diff_usd / bench_usd * 100, 4) if bench_usd else 0.0,
            "cash_ratio": round(cash_ratio, 4),
            "positions": len(ai_ledger.open_positions(state)),
        })
        ai_ledger.save_portfolio(state)
        log_lines.append("[AI-9] 台帳保存完了")
    else:
        log_lines.append("[AI-9] dry-runのため台帳保存はスキップ")

    return state, accepted_trades, log_lines, nav_usd, bench_usd, market_snapshots


def main() -> None:
    """3役だけを走らせて判断を確認する（発注も台帳更新も一切しない検証用）。

    daily_run.py の --dry-run は3役を呼ばない（毎回のdry-runでAPI費用を払わないため）。
    3役が実際に何を出すかを確かめたい時だけ、こちらを手で叩く。
    実行: python3 ai_daily.py --deliberate-only
    """
    import argparse
    import json

    import market as market_mod

    parser = argparse.ArgumentParser(description="AI裁量枠の3役を実走させる（発注・台帳更新はしない）")
    parser.add_argument(
        "--deliberate-only", action="store_true",
        help="探索役→検証役→執行役を走らせ、判断の中身を標準出力に出す（発注も記録もしない）",
    )
    args = parser.parse_args()
    if not args.deliberate_only:
        parser.error("--deliberate-only を指定してください（このスクリプトは単体では発注しません）")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    state = ai_ledger.load_portfolio()
    voo_snap = market_mod.get_snapshot(config.BENCHMARK_TICKER)
    voo_technicals = market_mod.get_voo_technicals(voo_snap.close)
    trade_date = voo_snap.date

    held = sorted(ai_ledger.open_positions(state))
    held_prices = fetch_prices(held) if held else {}
    prices = {t: v["close"] for t, v in held_prices.items()}
    nav = ai_ledger.compute_nav_usd(
        state, {t: TickerSnapshot(t, v["close"], v["date"]) for t, v in held_prices.items()},
    )
    bench = ai_ledger.compute_bench_nav_usd(state, voo_snap.close)

    tradable = ai_agents.fetch_tradable_universe()
    context = build_context(state, prices, voo_snap, voo_technicals, nav, bench, trade_date)
    record, logs = deliberate(context, tradable)
    for line in logs:
        print(line)

    raw_orders = record.get("orders", [])
    if raw_orders:
        need = sorted({str(o.get("ticker", "")).upper() for o in raw_orders} - set(prices))
        if need:
            prices = {**prices, **{t: v["close"] for t, v in fetch_prices(need).items()}}
        valid, rejected = ai_ledger.validate_orders(
            raw_orders, state, prices, set(tradable) if tradable else None, trade_date,
        )
        record["would_execute"] = valid
        record["rejected"] = rejected
    print("\n===== 判断の全文（decisions.jsonlに入る内容。今回は書き込んでいない） =====")
    print(json.dumps(record, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
