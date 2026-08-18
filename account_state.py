"""vs-sp500: 口座全体（本体・RSI枠で共有）の現金残高チェックポイント（2026-08-18 修正3）。

moomoo APIには手数料そのものを返す経路が無い（deal_list_query等を調査済み）。後日決済分
（settle_pending_orders）はbroker.get_cash()の前後差で実費を測れないため、実行の最後に
口座全体の現金残高を記録しておき、次回実行時に「前回との差分」から今回決済した全注文の
額面合計を差し引くことで手数料を逆算する。両枠（本体・RSI枠）で共有する値のため、
どちらの台帳にも入れずここに独立して置く。
"""
from __future__ import annotations

import json
import logging
from typing import Any

import config

logger = logging.getLogger("vs-sp500.account_state")

ACCOUNT_STATE_PATH = config.LEDGER_DIR / "account_state.json"

# 1回の決済で求めた手数料がこれを超えたら警告する。実測の手数料は5注文で$4.95（1注文$1前後）であり、
# $20を超えるのは手数料以外の何かが混ざった兆候。この閾値はFableが決めたもので、大将の指示ではない。
FEE_WARN_THRESHOLD_USD = 20.0


def load_account_cash() -> float | None:
    """前回記録した口座全体の現金残高。記録が無ければNone（初回）。"""
    if not ACCOUNT_STATE_PATH.exists():
        return None
    with open(ACCOUNT_STATE_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("account_cash_usd")


def save_account_cash(account_cash_usd: float, recorded_date: str) -> None:
    config.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = ACCOUNT_STATE_PATH.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(
            {"account_cash_usd": account_cash_usd, "recorded_date": recorded_date},
            f, ensure_ascii=False, indent=2,
        )
    tmp_path.replace(ACCOUNT_STATE_PATH)


def reconcile_fees(
    prev_account_cash: float | None,
    current_account_cash: float,
    settled_trades_by_book: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, float], list[str]]:
    """今回決済した全注文（両枠合算）の額面と口座現金の実増減から手数料を逆算し、額面比で按分する。

    settled_trades_by_book: {"main": [...], "rsi": [...]}。各要素はaction("BUY"/"SELL")と
    amount_usd（常に正の額面）を持つこと。

    手数料合計 = (current − prev) の符号を反転したもの − 今回決済した全注文の額面合計
    （額面合計はBUYを+・SELLを-として符号を付ける。BUYは現金減・SELLは現金増になるため）。

    戻り値: ({book名: 差し引くべき手数料USD}, ログ用メッセージのリスト)。
    初回（prev_account_cashがNone）は逆算せず全枠0を返す。
    """
    logs: list[str] = []
    fees_by_book = {book: 0.0 for book in settled_trades_by_book}

    if prev_account_cash is None:
        logs.append("account_state.json初回のため手数料逆算はスキップ（記録のみ）")
        return fees_by_book, logs

    settled_count = sum(len(trades) for trades in settled_trades_by_book.values())
    actual_cash_change = current_account_cash - prev_account_cash

    if settled_count == 0:
        if abs(actual_cash_change) > 1e-6:
            logs.append(
                f"決済0件だが口座現金が{actual_cash_change:+.2f}動いた"
                "（配当・手動売買の可能性。手数料としては計上せずログのみ）"
            )
        return fees_by_book, logs

    signed_face_value = sum(
        (1.0 if t["action"] == "BUY" else -1.0) * t["amount_usd"]
        for trades in settled_trades_by_book.values() for t in trades
    )
    fee_total = -actual_cash_change - signed_face_value

    if fee_total < -1e-6:
        logs.append(f"手数料逆算が負値（${fee_total:.4f}）になったため異常とみなし計上せずログのみ残す")
        return fees_by_book, logs

    if fee_total > FEE_WARN_THRESHOLD_USD:
        logs.append(f"⚠️ 手数料逆算が${fee_total:.2f}（$20超）。手数料以外の何かが混ざった可能性")

    volume_by_book = {
        book: sum(t["amount_usd"] for t in trades) for book, trades in settled_trades_by_book.items()
    }
    total_volume = sum(volume_by_book.values())
    if total_volume > 0:
        for book in fees_by_book:
            fees_by_book[book] = fee_total * (volume_by_book[book] / total_volume)

    logs.append(
        f"手数料逆算: 合計${fee_total:.4f}（決済{settled_count}件・実現金差${actual_cash_change:+.2f}） 按分: "
        + ", ".join(f"{book}=${fees_by_book[book]:.4f}" for book in fees_by_book)
    )
    return fees_by_book, logs
