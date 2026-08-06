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
                    "amount_jpy": {"type": "integer"},
                    "rule": {
                        "type": "string",
                        "enum": ["rebalance", "defense_switch", "defense_return", "dip_buy", "initial_build"],
                    },
                },
                "required": ["action", "ticker", "amount_jpy", "rule"],
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
)


def _build_user_prompt(
    charter_text: str,
    portfolio_state: dict[str, Any],
    market_snapshot: dict[str, TickerSnapshot],
    voo_technicals: Any,
    usdjpy_mid: float,
) -> str:
    holdings_weighted = []
    nav_hint = portfolio_state.get("cash_jpy", 0) + portfolio_state.get("cash_usd", 0) * usdjpy_mid
    for ticker, shares in portfolio_state.get("holdings", {}).items():
        snap = market_snapshot.get(ticker)
        if snap is None:
            continue
        currency = config.WHITELIST.get(ticker, {}).get("currency", "USD")
        value_jpy = shares * snap.close * (usdjpy_mid if currency == "USD" else 1.0)
        nav_hint += value_jpy
        holdings_weighted.append({"ticker": ticker, "shares": shares, "close": snap.close, "value_jpy": round(value_jpy)})

    market_lines = []
    for ticker, snap in market_snapshot.items():
        if ticker == config.FX_TICKER:
            continue
        market_lines.append({"ticker": ticker, "close": snap.close, "date": snap.date})

    payload = {
        "portfolio": {
            "mode": portfolio_state.get("mode"),
            "cash_jpy": portfolio_state.get("cash_jpy"),
            "cash_usd": portfolio_state.get("cash_usd"),
            "holdings": holdings_weighted,
            "estimated_nav_jpy": round(nav_hint),
            "above_200dma_streak": portfolio_state.get("above_200dma_streak"),
            "below_200dma_streak": portfolio_state.get("below_200dma_streak"),
        },
        "market": {
            "usdjpy_mid": usdjpy_mid,
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
    usdjpy_mid: float,
) -> dict[str, Any]:
    """Sonnetに売買判断を問い合わせる。失敗時は1回リトライし、それでも失敗ならホールド扱い。"""
    user_prompt = _build_user_prompt(charter_text, portfolio_state, market_snapshot, voo_technicals, usdjpy_mid)
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
