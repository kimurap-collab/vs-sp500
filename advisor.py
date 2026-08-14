"""vs-sp500: Sonnet呼び出し（投資憲章の番兵としての売買判断）。"""
from __future__ import annotations

import json
import logging
from typing import Any

from anthropic import Anthropic

import config
from market import TickerSnapshot

logger = logging.getLogger("vs-sp500.advisor")

TRADE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "trades": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["BUY", "SELL"]},
                    "ticker": {"type": "string"},
                    "amount_usd": {"type": "number"},
                    "rule": {
                        "type": "string",
                        "enum": ["rebalance", "defense_switch", "defense_return", "dip_buy", "initial_build"],
                    },
                },
                "required": ["action", "ticker", "amount_usd", "rule"],
                "additionalProperties": False,
            },
        },
        "mode": {"type": "string", "enum": ["normal", "defense"]},
        "reason": {"type": "string"},
    },
    "required": ["trades", "mode", "reason"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = (
    "あなたは投資憲章の番兵です。憲章の発動条件に該当する場合のみ、"
    "該当ルールに基づく取引を提案してください。該当しなければtradesは空配列にしてください。"
    "憲章にない取引の提案は禁止です。迷ったらホールドしてください。"
    "リバランスでの買い増し禁止は個別株（type=stock）だけに適用される追加ルールであり、"
    "ETFには適用しない。ETFがターゲット配分を下回っている場合は、"
    "ターゲットに戻す買い増しをrebalanceとして提案してよい（これが正規の動作である）。"
    "個別株だけは下振れしても買い増しを提案するな（放置でよい）。"
    "上振れ分の利確売りは個別株・ETFとも提案してよい。"
    "損切りはコード側が自動執行するのでお前が提案する必要はない。"
    "株数は整数単位でしか約定できない（端株は買えない）。amount_usdは目安の金額でよく、"
    "実際の約定株数は現在値で切り捨てた整数株になる。"
)


def _build_user_prompt(
    charter_text: str,
    portfolio_state: dict[str, Any],
    market_snapshot: dict[str, TickerSnapshot],
    voo_technicals: Any,
) -> str:
    holdings_weighted = []
    nav_hint = portfolio_state.get("cash_usd", 0)
    for ticker, shares in portfolio_state.get("holdings", {}).items():
        snap = market_snapshot.get(ticker)
        if snap is None:
            continue
        value_usd = shares * snap.close
        nav_hint += value_usd
        ticker_type = "stock" if config.is_stock(ticker) else "etf"
        holdings_weighted.append({
            "ticker": ticker, "type": ticker_type, "shares": shares,
            "close": snap.close, "value_usd": round(value_usd, 2),
        })

    market_lines = []
    for ticker, snap in market_snapshot.items():
        if ticker == config.FX_TICKER:
            continue
        market_lines.append({"ticker": ticker, "close": snap.close, "date": snap.date})

    payload = {
        "portfolio": {
            "mode": portfolio_state.get("mode"),
            "cash_usd": portfolio_state.get("cash_usd"),
            "holdings": holdings_weighted,
            "estimated_nav_usd": round(nav_hint, 2),
            "above_200dma_streak": portfolio_state.get("above_200dma_streak"),
            "below_200dma_streak": portfolio_state.get("below_200dma_streak"),
        },
        "market": {
            "tickers": market_lines,
            "voo_ma200": voo_technicals.ma200,
            "voo_rsi14": voo_technicals.rsi14,
            "voo_high_52w": voo_technicals.high_52w,
        },
    }

    return (
        "## 投資憲章\n" + charter_text + "\n\n"
        "## 現在のポートフォリオと市場スナップショット\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + "\n\n上記に基づき、本日の売買判断をJSONで出力してください。"
    )


def get_trade_decision(
    charter_text: str,
    portfolio_state: dict[str, Any],
    market_snapshot: dict[str, TickerSnapshot],
    voo_technicals: Any,
) -> dict[str, Any]:
    """Sonnetに売買判断を問い合わせる。失敗時は1回リトライし、それでも失敗ならホールド扱い。"""
    user_prompt = _build_user_prompt(charter_text, portfolio_state, market_snapshot, voo_technicals)
    client = Anthropic(api_key=config.CLAUDE_API_KEY)

    last_error: Exception | None = None
    for attempt in range(2):
        try:
            response = client.messages.create(
                model=config.ANTHROPIC_MODEL,
                max_tokens=config.ANTHROPIC_MAX_TOKENS,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
                output_config={
                    "format": {"type": "json_schema", "schema": TRADE_SCHEMA},
                    "effort": config.ANTHROPIC_EFFORT,
                },
            )
            text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
            parsed = json.loads(text)
            return parsed
        except Exception as e:  # noqa: BLE001 - API障害全般をリトライ・フォールバック対象にする
            last_error = e
            logger.warning("advisor呼び出し失敗（試行%d）: %s", attempt + 1, e)

    logger.error("advisor呼び出しが2回とも失敗。ホールド扱い: %s", last_error)
    return {
        "trades": [],
        "mode": portfolio_state.get("mode", "normal"),
        "reason": f"API呼び出し失敗によりホールド: {last_error}",
    }
