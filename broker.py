"""vs-sp500: moomoo APIの薄いラッパ。外部依存(moomoo SDK)をここに閉じ込める。

moomoo SDKは無応答で固まる実績があるため、全呼び出しをワーカースレッド上で実行し
timeout付きjoinで見切りをつける（signal.alarmでは止められないことを実測で確認済み。
2026-08-14: signal.alarm(8)を設定してもaccinfo_queryが120秒超ブロックし続けた）。
タイムアウト時、ワーカースレッドはdaemon=Trueのため呼び出し元をブロックし続けず、
プロセス終了時に道連れで破棄される。
"""
from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Any, Callable

import config

logger = logging.getLogger("vs-sp500.broker")

CONNECT_CHECK_TIMEOUT_SEC = 3.0
CALL_TIMEOUT_SEC = 15.0
ORDER_FILL_TIMEOUT_SEC = 30.0
ORDER_POLL_INTERVAL_SEC = 1.0

# 約定を待たずに終了とみなす注文の終端ステータス（FILLED_ALL以外）
_ORDER_TERMINAL_FAILURE_STATUSES = (
    "FAILED", "SUBMIT_FAILED", "CANCELLED_ALL", "DISABLED", "DELETED", "FILL_CANCELLED", "TIMEOUT",
)


def is_available() -> bool:
    """OpenD (127.0.0.1:11111) にTCPで到達できるか確認する。

    moomoo SDK自体は接続失敗時に無限リトライする実績があるため、SDKを呼ぶ前に
    ここで生死を高速判定する（これが呼び出し元にとって唯一の高速な縮退判定手段）。
    """
    try:
        with socket.create_connection(
            (config.MOOMOO_HOST, config.MOOMOO_PORT), timeout=CONNECT_CHECK_TIMEOUT_SEC
        ):
            return True
    except OSError as e:
        logger.warning("OpenD未接続: %s", e)
        return False


def _run_with_timeout(fn: Callable[[], Any], timeout: float = CALL_TIMEOUT_SEC) -> Any | None:
    """関数をデーモンスレッドで実行し、timeout秒でjoinを諦めて呼び出し元に制御を返す。

    タイムアウトした場合、スレッド自体は残存する可能性がある（moomoo SDKが本当に
    無応答なケース）が、daemon=Trueなのでプロセス終了は妨げない。
    """
    result: dict[str, Any] = {}
    error: dict[str, Exception] = {}

    def _target() -> None:
        try:
            result["value"] = fn()
        except Exception as e:  # noqa: BLE001 - moomoo SDK内部の例外型は不定
            error["value"] = e

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    if thread.is_alive():
        logger.error("moomoo呼び出しがタイムアウトした（%s秒）", timeout)
        return None
    if "value" in error:
        logger.error("moomoo呼び出しが例外を送出した: %s", error["value"])
        return None
    return result.get("value")


def _open_trade_ctx():
    from moomoo import OpenSecTradeContext, SecurityFirm, TrdMarket
    return OpenSecTradeContext(
        filter_trdmarket=TrdMarket.US,
        host=config.MOOMOO_HOST,
        port=config.MOOMOO_PORT,
        security_firm=SecurityFirm.FUTUSG,
    )


def get_positions() -> dict[str, float] | None:
    """{'VOO': 31.0, ...} を返す。失敗時None。"""

    def _call() -> dict[str, float]:
        from moomoo import TrdEnv

        ctx = _open_trade_ctx()
        try:
            ret, data = ctx.position_list_query(trd_env=TrdEnv.SIMULATE, acc_id=config.MOOMOO_ACC_ID)
            if ret != 0:
                raise RuntimeError(f"position_list_query失敗: {data}")
            positions: dict[str, float] = {}
            for row in data.to_dict(orient="records"):
                code = row["code"]  # 例: 'US.VOO'
                ticker = code.split(".", 1)[1] if "." in code else code
                positions[ticker] = float(row["qty"])
            return positions
        finally:
            ctx.close()

    return _run_with_timeout(_call)


def get_cash() -> float | None:
    """口座の現金(USD)。失敗時None。"""

    def _call() -> float:
        from moomoo import TrdEnv

        ctx = _open_trade_ctx()
        try:
            ret, data = ctx.accinfo_query(trd_env=TrdEnv.SIMULATE, acc_id=config.MOOMOO_ACC_ID, currency="USD")
            if ret != 0:
                raise RuntimeError(f"accinfo_query失敗: {data}")
            return float(data.iloc[0]["cash"])
        finally:
            ctx.close()

    return _run_with_timeout(_call)


def get_snapshot(tickers: list[str]) -> dict[str, float] | None:
    """現在値（照合用）。{'VOO': 714.95, ...}。失敗時None。"""

    def _call() -> dict[str, float]:
        from moomoo import OpenQuoteContext

        ctx = OpenQuoteContext(host=config.MOOMOO_HOST, port=config.MOOMOO_PORT)
        try:
            codes = [f"US.{t}" for t in tickers]
            ret, data = ctx.get_market_snapshot(codes)
            if ret != 0:
                raise RuntimeError(f"get_market_snapshot失敗: {data}")
            result: dict[str, float] = {}
            for row in data.to_dict(orient="records"):
                code = row["code"]
                ticker = code.split(".", 1)[1] if "." in code else code
                result[ticker] = float(row["last_price"])
            return result
        finally:
            ctx.close()

    return _run_with_timeout(_call)


def place_market_order(ticker: str, qty: int, side: str) -> dict[str, Any] | None:
    """成行注文を出し、約定まで待つ。

    戻り値: {'filled_qty': int, 'avg_price': float}。失敗（拒否・タイムアウト・未約定）時None。
    """
    if side not in ("BUY", "SELL"):
        raise ValueError(f"不正なside: {side}")
    if qty <= 0:
        raise ValueError(f"不正なqty: {qty}")

    def _call() -> dict[str, Any]:
        from moomoo import OrderType, TrdEnv, TrdSide

        ctx = _open_trade_ctx()
        try:
            trd_side = TrdSide.BUY if side == "BUY" else TrdSide.SELL
            # fill_outside_rth=True が必須: daily_run.pyは毎朝7時JST（≒米国市場RTH終了の
            # 約2時間後）に実行される。RTH限定のままだと注文はFILLED_ALLにならずSUBMITTEDの
            # まま張り付き、約定待ちが必ずタイムアウトする（2026-08-14実測。EWJ移行注文が
            # dealt_qty=0のままSUBMITTEDで固まったことで発覚）。
            ret, data = ctx.place_order(
                price=0,
                qty=qty,
                code=f"US.{ticker}",
                trd_side=trd_side,
                order_type=OrderType.MARKET,
                trd_env=TrdEnv.SIMULATE,
                acc_id=config.MOOMOO_ACC_ID,
                fill_outside_rth=True,
            )
            if ret != 0:
                raise RuntimeError(f"place_order失敗: {data}")
            order_id = str(data.iloc[0]["order_id"])

            deadline = time.monotonic() + ORDER_FILL_TIMEOUT_SEC
            while time.monotonic() < deadline:
                ret, orders = ctx.order_list_query(
                    order_id=order_id, trd_env=TrdEnv.SIMULATE, acc_id=config.MOOMOO_ACC_ID
                )
                if ret == 0 and not orders.empty:
                    row = orders.iloc[0]
                    status = row["order_status"]
                    if status == "FILLED_ALL":
                        return {
                            "filled_qty": int(float(row["dealt_qty"])),
                            "avg_price": float(row["dealt_avg_price"]),
                        }
                    if status in _ORDER_TERMINAL_FAILURE_STATUSES:
                        raise RuntimeError(f"注文が約定せず終端した: status={status} order_id={order_id}")
                time.sleep(ORDER_POLL_INTERVAL_SEC)
            raise RuntimeError(f"注文の約定待ちがタイムアウトした（{ORDER_FILL_TIMEOUT_SEC}秒）: order_id={order_id}")
        finally:
            ctx.close()

    return _run_with_timeout(_call, timeout=ORDER_FILL_TIMEOUT_SEC + 10)
