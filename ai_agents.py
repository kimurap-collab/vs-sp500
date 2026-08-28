"""vs-sp500: AI裁量枠の3役（探索役・検証役・執行役）とmoomooスクリーナー。

大将の指示（2026-08-27〜28）:
  「取引する時間帯も、取引量も、君が利用するaiエージェントの数も、購入や売却の決定も、
    全て君たちがやるんだ。俺の承認は不要だ」
  「運用チームを作るのも君で、自律改善するのも勝手だ」

**このファイルのプロンプトに売買ルールを書いてはいけない。** 何を探すか・いくら買うか・
いつ売るかを決めるのがAIの仕事であり、そこを人間が書いた時点でこの枠の主題が消える。
ここに書いてよいのは「役割の定義」「出力の形式」「守るべきガードレール」だけである。

役の構成（Fableが決定。大将「aiエージェントの数も君たちがやる」）:
  1. 探索役A（絞り込み条件の決定）… 今日どんな条件で銘柄を絞るかを自分で決める
  2. 探索役B（候補の選定）        … スクリーナー結果から候補を出し、買う理由を書く
  3. 検証役（敵対的レビュー）      … 候補を潰しにかかる。既定は「買わない」
  4. 執行役（発注量の決定と記録）  … 生き残った候補について金額を決め、理由を残す
  探索役を2回に分けているのは、スクリーナーの結果を見ずに条件だけ先に決めさせるため
  （結果を見てから条件を後付けすると「絞り込み条件を自分で決める」が成立しない）。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import threading
from typing import Any, Callable

from anthropic import Anthropic

import broker
import config

logger = logging.getLogger("vs-sp500.ai_agents")

_MOOMOO_CALL_TIMEOUT_SEC = 20.0

# moomooのスクリーナーで実際に使えることを実機で確認した項目だけを載せる
# （2026-08-28 実測。VOLUME・TURNOVER・TURNOVER_RATE・CHANGE_RATE・AMPLITUDE は
#  "This filter field is not supported." が返るため載せない）。
# 単位・符号は2026-08-28に実データで確認した。ここを間違えると条件が0件になって
# 1日まるごと無駄になるため、推測で書かず実測値の例を添える。
SIMPLE_FIELDS: dict[str, str] = {
    "MARKET_VAL": "時価総額(USD。例: 100億ドルなら10000000000)",
    "FLOAT_MARKET_VAL": "浮動株時価総額(USD)",
    "CUR_PRICE": "現在値(USD)",
    "PE_TTM": "PER(TTM。倍。例: 21.6)",
    "PB_RATE": "PBR(倍。赤字企業は負の値になりうる。例: 2.76)",
    "PS_TTM": "PSR(TTM。倍)",
    "PCF_TTM": "PCFR(TTM。倍)",
    "VOLUME_RATIO": "出来高比率(倍。1.0で平常。例: 1.43)",
    "BID_ASK_RATIO": "委託売買比率(%。取れない銘柄は0.0)",
    "CHANGE_RATE_BEGIN_YEAR": "年初来騰落率(%。例: +41.49 / -19.63)",
    "CUR_PRICE_TO_HIGHEST52_WEEKS_RATIO":
        "**52週高値からの乖離率(%。高値そのものが0で、下にあるほど負)**。"
        "例: -2.08は高値のほぼ真下、-45.97は高値から46%安。"
        "『高値圏を買う』ならmin=-10、『高値から2割以上下げたもの』ならmax=-20とする",
    "CUR_PRICE_TO_LOWEST52_WEEKS_RATIO":
        "**52週安値からの上昇率(%。安値が0で、上にあるほど正)**。"
        "例: +7.15は安値のすぐ上、+583.76は安値から6.8倍",
}

FINANCIAL_FIELDS: dict[str, str] = {
    "NET_PROFIX_GROWTH": "純利益成長率(%)",
    "SUM_OF_BUSINESS_GROWTH": "売上成長率(%)",
    "EPS_GROWTH_RATE": "EPS成長率(%)",
    "RETURN_ON_EQUITY_RATE": "ROE(%)",
    "ROA_TTM": "ROA(TTM %)",
    "NET_PROFIT_RATE": "純利益率(%)",
    "GROSS_PROFIT_RATE": "粗利率(%)",
    "OPERATING_MARGIN_TTM": "営業利益率(TTM %)",
    "DEBT_ASSET_RATE": "負債比率(%)",
    "CURRENT_RATIO": "流動比率(%)",
}

INDICATOR_FIELDS: dict[str, str] = {
    "PRICE": "終値",
    "RSI": "RSI（paraで期間指定。例[14]）",
    "MA": "単純移動平均（paraで期間指定。例[60]）",
    "EMA": "指数移動平均（paraで期間指定）",
    "MACD_DIFF": "MACD DIFF（para例[12,26,9]）",
    "MACD_DEA": "MACD DEA（para例[12,26,9]）",
    "KDJ_K": "KDJのK（para例[9,3,3]）",
    "BOLL_UPPER": "ボリンジャー上限（para例[20,2]）",
    "BOLL_LOWER": "ボリンジャー下限（para例[20,2]）",
    "VALUE": "定数（valueで数値を指定する時に右辺として使う）",
}

QUARTERS = ["ANNUAL", "MOST_RECENT_QUARTER", "INTERIM", "FIRST_QUARTER", "THIRD_QUARTER"]


def _run_with_timeout(fn: Callable[[], Any], timeout: float = _MOOMOO_CALL_TIMEOUT_SEC) -> Any | None:
    """broker.pyと同じ方式: デーモンスレッドで実行しtimeoutで見切りをつける。"""
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


# ---------------------------------------------------------------------------
# 売買してよい銘柄の集合（ガードレール用）
# ---------------------------------------------------------------------------

_TRADABLE_CACHE: dict[str, str] | None = None


def fetch_tradable_universe() -> dict[str, str] | None:
    """moomooで売買できる米国の普通株（NASDAQ/NYSE/AMEX・上場中）を {ticker: 銘柄名} で返す。

    この集合が「買ってよい銘柄」のガードレールになる。設計上の判断（Fable決定）:
      - **ETFを含めない。** レバレッジ型・インバース型ETF（TQQQ・SQQQ等）を機械的に締め出す
        最も確実な方法であり、憲章の「レバレッジ・空売り禁止」をコードで守れる形にするため。
        moomooではETFは SecurityType.ETF に分離されているので、STOCKだけを取れば自動的に外れる。
      - **US_PINK（店頭・ADRの大半）を含めない。** スクリーナーはPINK銘柄も返してくるが
        （2026-08-28実測: 全13,060銘柄中5,897件がPINK）、仮想口座で約定するとは限らない。
    プロセス内で1回だけ取得してメモ化する（get_stock_basicinfoは日足の1000銘柄枠を消費しない）。
    """
    global _TRADABLE_CACHE
    if _TRADABLE_CACHE is not None:
        return _TRADABLE_CACHE

    def _call() -> dict[str, str]:
        from moomoo import Market, OpenQuoteContext, SecurityType

        ctx = OpenQuoteContext(host=config.MOOMOO_HOST, port=config.MOOMOO_PORT)
        try:
            ret, data = ctx.get_stock_basicinfo(market=Market.US, stock_type=SecurityType.STOCK)
            if ret != 0:
                raise RuntimeError(f"get_stock_basicinfo(US, STOCK)失敗: {data}")
            allowed = {"US_NASDAQ", "US_NYSE", "US_AMEX"}
            result: dict[str, str] = {}
            for row in data.to_dict(orient="records"):
                if str(row.get("exchange_type")) not in allowed:
                    continue
                if bool(row.get("delisting")):
                    continue
                result[broker.code_to_ticker(str(row["code"]))] = str(row.get("name") or "")
            return result
        finally:
            ctx.close()

    universe = _run_with_timeout(_call)
    if universe is None:
        logger.error("売買可能銘柄の一覧(get_stock_basicinfo)の取得に失敗した")
        return None
    _TRADABLE_CACHE = universe
    logger.info("売買可能な米国普通株: %d銘柄", len(universe))
    return universe


# ---------------------------------------------------------------------------
# スクリーナー（探索役が決めた条件で叩く）
# ---------------------------------------------------------------------------

def _build_filters(plan: dict[str, Any]) -> list[Any]:
    """探索役の絞り込み条件をmoomooのフィルタ objectのリストに変換する。

    未知のフィールド・壊れた指定は黙って落とす（1項目の誤りで探索全体を止めないため。
    落としたことはログに残す）。
    """
    from moomoo import (
        CustomIndicatorFilter,
        FinancialFilter,
        FinancialQuarter,
        KLType,
        RelativePosition,
        SimpleFilter,
        SortDir,
        StockField,
    )

    filters: list[Any] = []
    used_simple_fields: set[str] = set()
    sort_taken = False

    for item in plan.get("simple_filters", []) or []:
        field = str(item.get("field", "")).upper()
        if field not in SIMPLE_FIELDS:
            logger.warning("探索条件を無視した（未知のsimple field）: %s", field)
            continue
        if field in used_simple_fields:
            logger.warning("探索条件を無視した（同じfieldの重複指定はmoomooが拒否する）: %s", field)
            continue
        used_simple_fields.add(field)
        f = SimpleFilter()
        f.stock_field = getattr(StockField, field)
        f.is_no_filter = False
        if item.get("min") is not None:
            f.filter_min = float(item["min"])
        if item.get("max") is not None:
            f.filter_max = float(item["max"])
        sort = str(item.get("sort") or "").lower()
        if sort in ("asc", "desc") and not sort_taken:
            f.sort = SortDir.ASCEND if sort == "asc" else SortDir.DESCEND
            sort_taken = True
        filters.append(f)

    for item in plan.get("financial_filters", []) or []:
        field = str(item.get("field", "")).upper()
        if field not in FINANCIAL_FIELDS:
            logger.warning("探索条件を無視した（未知のfinancial field）: %s", field)
            continue
        f = FinancialFilter()
        f.stock_field = getattr(StockField, field)
        f.is_no_filter = False
        quarter = str(item.get("quarter") or "ANNUAL").upper()
        f.quarter = getattr(FinancialQuarter, quarter if quarter in QUARTERS else "ANNUAL")
        if item.get("min") is not None:
            f.filter_min = float(item["min"])
        if item.get("max") is not None:
            f.filter_max = float(item["max"])
        filters.append(f)

    for item in plan.get("indicator_filters", []) or []:
        left = str(item.get("left", "")).upper()
        right = str(item.get("right", "")).upper()
        op = str(item.get("op", "")).upper()
        if left not in INDICATOR_FIELDS or right not in INDICATOR_FIELDS:
            logger.warning("探索条件を無視した（未知のindicator field）: %s / %s", left, right)
            continue
        if op not in ("MORE", "LESS", "CROSS_UP", "CROSS_DOWN"):
            logger.warning("探索条件を無視した（未知のop）: %s", op)
            continue
        f = CustomIndicatorFilter()
        f.ktype = KLType.K_DAY
        f.is_no_filter = False
        f.stock_field1 = getattr(StockField, left)
        if item.get("left_para"):
            f.stock_field1_para = [int(x) for x in item["left_para"]]
        f.stock_field2 = getattr(StockField, right)
        if item.get("right_para"):
            f.stock_field2_para = [int(x) for x in item["right_para"]]
        if right == "VALUE":
            if item.get("value") is None:
                logger.warning("探索条件を無視した（right=VALUEなのにvalueが無い）")
                continue
            f.value = float(item["value"])
        f.relative_position = getattr(RelativePosition, op)
        filters.append(f)

    return filters


def run_screener(plan: dict[str, Any], tradable: dict[str, str] | None) -> list[dict[str, Any]] | None:
    """探索役が決めた条件でmoomooスクリーナーを叩く。失敗時None・該当0件は空リスト。

    moomooが条件を拒否した場合（未対応フィールドの組み合わせ等）は、条件を末尾から1つずつ
    落として再試行する。全部落ちたら時価総額の下限だけで叩く（探索を丸ごと諦めない）。
    """
    from moomoo import Market, OpenQuoteContext, SimpleFilter, SortDir, StockField

    filters = _build_filters(plan)
    if not filters:
        f = SimpleFilter()
        f.stock_field = StockField.MARKET_VAL
        f.filter_min = 1_000_000_000.0
        f.is_no_filter = False
        f.sort = SortDir.DESCEND
        filters = [f]
        logger.warning("探索条件が1つも成立しなかったため、時価総額10億ドル以上のみで探索する")

    max_rows = min(int(plan.get("max_results") or config.AI_SCREENER_MAX_ROWS), config.AI_SCREENER_MAX_ROWS)

    def _call(active: list[Any]) -> tuple[int, Any]:
        ctx = OpenQuoteContext(host=config.MOOMOO_HOST, port=config.MOOMOO_PORT)
        try:
            return ctx.get_stock_filter(
                market=Market.US, filter_list=active, begin=0, num=config.AI_SCREENER_PAGE_SIZE,
            )
        finally:
            ctx.close()

    active = list(filters)
    result = None
    while active:
        outcome = _run_with_timeout(lambda a=list(active): _call(a))
        if outcome is None:
            return None
        ret, data = outcome
        if ret == 0:
            result = data
            break
        logger.warning("スクリーナーが条件を拒否した（%s）。条件を1つ落として再試行する", data)
        active.pop()
    if result is None:
        logger.error("スクリーナーが全ての条件で失敗した")
        return None

    _last_page, _all_count, rows = result
    screened: list[dict[str, Any]] = []
    for row in rows:
        ticker = broker.code_to_ticker(str(row.stock_code))
        if tradable is not None and ticker not in tradable:
            continue
        metrics: dict[str, Any] = {}
        for key, value in row.__dict__.items():
            if key in ("stock_code", "stock_name"):
                continue
            label = "_".join(str(k) for k in key) if isinstance(key, tuple) else str(key)
            metrics[label] = round(value, 4) if isinstance(value, float) else value
        screened.append({"ticker": ticker, "name": row.stock_name, "metrics": metrics})
        if len(screened) >= max_rows:
            break
    return screened


# ---------------------------------------------------------------------------
# LLM呼び出しの共通部分
# ---------------------------------------------------------------------------

def _call_model(
    system: str, user: str, schema: dict[str, Any], label: str,
) -> tuple[dict[str, Any] | None, dict[str, int]]:
    """構造化出力でモデルを呼ぶ。失敗時は1回リトライし、それでも駄目ならNone。

    戻り値: (parsed, {"input_tokens": .., "output_tokens": ..})
    """
    client = Anthropic(api_key=config.CLAUDE_API_KEY)
    usage = {"input_tokens": 0, "output_tokens": 0}
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            response = client.messages.create(
                model=config.AI_DESK_MODEL,
                max_tokens=config.AI_DESK_MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={
                    "format": {"type": "json_schema", "schema": schema},
                    "effort": config.AI_DESK_EFFORT,
                },
            )
            if getattr(response, "usage", None) is not None:
                usage["input_tokens"] += getattr(response.usage, "input_tokens", 0) or 0
                usage["output_tokens"] += getattr(response.usage, "output_tokens", 0) or 0
            text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
            return json.loads(text), usage
        except Exception as e:  # noqa: BLE001 - API障害全般をリトライ対象にする
            last_error = e
            logger.warning("%s の呼び出しに失敗（試行%d）: %s", label, attempt + 1, e)
    logger.error("%s の呼び出しが2回とも失敗した: %s", label, last_error)
    return None, usage


# ---------------------------------------------------------------------------
# 1. 探索役A: 今日の絞り込み条件を決める
# ---------------------------------------------------------------------------

SCOUT_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rationale": {"type": "string"},
        "simple_filters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string", "enum": sorted(SIMPLE_FIELDS)},
                    "min": {"type": ["number", "null"]},
                    "max": {"type": ["number", "null"]},
                    # nullableなenumはAPIが受け付けないため、「並べ替えなし」をnoneという値で表す
                    "sort": {"type": "string", "enum": ["asc", "desc", "none"]},
                },
                "required": ["field", "min", "max", "sort"],
                "additionalProperties": False,
            },
        },
        "financial_filters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string", "enum": sorted(FINANCIAL_FIELDS)},
                    "min": {"type": ["number", "null"]},
                    "max": {"type": ["number", "null"]},
                    "quarter": {"type": "string", "enum": QUARTERS},
                },
                "required": ["field", "min", "max", "quarter"],
                "additionalProperties": False,
            },
        },
        "indicator_filters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "left": {"type": "string", "enum": sorted(INDICATOR_FIELDS)},
                    "left_para": {"type": "array", "items": {"type": "integer"}},
                    "op": {"type": "string", "enum": ["MORE", "LESS", "CROSS_UP", "CROSS_DOWN"]},
                    "right": {"type": "string", "enum": sorted(INDICATOR_FIELDS)},
                    "right_para": {"type": "array", "items": {"type": "integer"}},
                    "value": {"type": ["number", "null"]},
                },
                "required": ["left", "left_para", "op", "right", "right_para", "value"],
                "additionalProperties": False,
            },
        },
        "max_results": {"type": "integer"},
    },
    "required": ["rationale", "simple_filters", "financial_filters", "indicator_filters", "max_results"],
    "additionalProperties": False,
}

SCOUT_PLAN_SYSTEM = (
    "あなたは米国株の運用チームの探索役です。今日どんな条件で銘柄を絞り込むかを"
    "**あなた自身が決める**のが仕事です。条件は毎日同じである必要はなく、"
    "相場の状況・現在の保有・過去の判断とその結果を踏まえて自由に変えてかまいません。\n"
    "スクリーナーはmoomooのもので、使えるのは提示された項目だけです。"
    "条件が緩すぎると候補が絞れず、厳しすぎると0件になります。"
    "rationaleには「なぜ今日この条件で探すのか」を具体的に書いてください。"
)

# 探索・検証・執行に共通で渡す前提（この枠が守るべき制約。売買ルールではない）
CONSTRAINTS_TEXT = (
    "## この枠の制約（守れないものはコード側で自動的に弾かれます）\n"
    "- moomooの**仮想口座**での取引です（実弾ではありません）。約定価格も手数料も証券会社が決めます。\n"
    "- 買えるのは**米国の主要取引所（NASDAQ/NYSE/AMEX）に上場する普通株**だけです。"
    "ETF・レバレッジ商品・店頭(OTC/Pink)銘柄は買えません。\n"
    "- **空売り・信用取引・レバレッジは禁止**です。売れるのはこの枠が保有している株だけです。\n"
    "- **現金を超える買いはできません。**\n"
    f"- 1日に出せる注文は**最大{config.AI_MAX_ORDERS_PER_DAY}件**です。\n"
    "- 株数は整数単位です（端株は買えません）。\n"
    "- デイトレードは想定していません。**数日〜数週間のスイング**で、判断は1日1回です。"
    "1注文あたり$1前後の手数料がかかるため、回転させるほどコストで負けます。\n"
)


def _context_block(context: dict[str, Any]) -> str:
    return "## 現在の状況（この枠の台帳・市場・過去の判断）\n" + json.dumps(
        context, ensure_ascii=False, indent=2, default=str,
    )


def scout_plan(context: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, int]]:
    fields_doc = {
        "simple_filters": SIMPLE_FIELDS,
        "financial_filters": FINANCIAL_FIELDS,
        "indicator_filters": INDICATOR_FIELDS,
    }
    user = (
        CONSTRAINTS_TEXT + "\n"
        + _context_block(context) + "\n\n"
        "## 使えるスクリーナー項目\n"
        + json.dumps(fields_doc, ensure_ascii=False, indent=2) + "\n\n"
        "指標フィルタ(indicator_filters)は「左辺 op 右辺」で書きます。"
        "右辺にVALUEを選んだ場合はvalueに数値を入れてください（例: RSI(14) LESS VALUE 35）。"
        "左辺・右辺に指標を選べば指標同士の比較になります（例: PRICE MORE MA[60]）。\n"
        f"max_resultsは最大{config.AI_SCREENER_MAX_ROWS}件までです。\n\n"
        "今日の絞り込み条件をJSONで出力してください。"
    )
    return _call_model(SCOUT_PLAN_SYSTEM, user, SCOUT_PLAN_SCHEMA, "探索役A(絞り込み条件)")


# ---------------------------------------------------------------------------
# 2. 探索役B: 候補を出す
# ---------------------------------------------------------------------------

SCOUT_PICK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "buy_candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "thesis": {"type": "string"},
                    "what_i_saw": {"type": "string"},
                    "horizon_days": {"type": "integer"},
                    "conviction": {"type": "integer"},
                },
                "required": ["ticker", "thesis", "what_i_saw", "horizon_days", "conviction"],
                "additionalProperties": False,
            },
        },
        "sell_candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "thesis": {"type": "string"},
                    "what_i_saw": {"type": "string"},
                },
                "required": ["ticker", "thesis", "what_i_saw"],
                "additionalProperties": False,
            },
        },
        "market_read": {"type": "string"},
    },
    "required": ["buy_candidates", "sell_candidates", "market_read"],
    "additionalProperties": False,
}

SCOUT_PICK_SYSTEM = (
    "あなたは米国株の運用チームの探索役です。スクリーナーの結果と、現在の保有・過去の判断と"
    "その結果を見て、**買う候補**と、保有のうち**手放す候補**を挙げるのが仕事です。\n"
    "この枠の勝ち目は速度でも価格パターンでもなく、**情報の統合**にあります"
    "（何が起きている会社か、他の銘柄・セクターとどう繋がるか、市場が何を織り込んでいるか）。\n"
    "候補を無理に挙げる必要はありません。**買うものが無ければ空でかまいません。**\n"
    "thesisとwhat_i_sawには「そのとき何を見て何を考えたか」を具体的に書いてください。"
    "『割安だから』『好材料が出ているから』のような中身のない文言は禁止です。"
    "数字を見たならその数字を、比較したなら比較対象を書いてください。\n"
    "convictionは1〜5の確信度、horizon_daysは想定保有日数です。"
)


def scout_pick(
    context: dict[str, Any], screened: list[dict[str, Any]], plan: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, int]]:
    user = (
        CONSTRAINTS_TEXT + "\n"
        + _context_block(context) + "\n\n"
        "## あなたが今日決めた絞り込み条件\n"
        + json.dumps(plan, ensure_ascii=False, indent=2) + "\n\n"
        f"## スクリーナーの結果（{len(screened)}件）\n"
        + json.dumps(screened, ensure_ascii=False, indent=2) + "\n\n"
        f"買う候補は最大{config.AI_MAX_CANDIDATES}件までにしてください。"
        "売る候補は現在の保有銘柄からのみ挙げられます。JSONで出力してください。"
    )
    return _call_model(SCOUT_PICK_SYSTEM, user, SCOUT_PICK_SCHEMA, "探索役B(候補選定)")


# ---------------------------------------------------------------------------
# 3. 検証役: 候補を潰す
# ---------------------------------------------------------------------------

CHALLENGER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "side": {"type": "string", "enum": ["BUY", "SELL"]},
                    "verdict": {"type": "string", "enum": ["kill", "survive"]},
                    "attack": {"type": "string"},
                },
                "required": ["ticker", "side", "verdict", "attack"],
                "additionalProperties": False,
            },
        },
        "overall": {"type": "string"},
    },
    "required": ["verdicts", "overall"],
    "additionalProperties": False,
}

CHALLENGER_SYSTEM = (
    "あなたは運用チームの検証役です。探索役が挙げた候補を**潰すのが仕事**です。"
    "買わない理由・売らない理由を探してください。\n"
    "**既定は『買わない』です。** あなたが致命的な反論を出せなかった候補だけが生き残ります。"
    "迷ったらkillにしてください。全部killでかまいません。\n"
    "attackには具体的な反論を書いてください（テーゼの前提が事実か、既に株価に織り込まれていないか、"
    "同じ材料でもっと良い銘柄がないか、下振れした場合に何が起きるか、"
    "手数料と想定リターンが釣り合うか）。\n"
    "surviveにする場合のattackには「どう潰そうとして、なぜ潰せなかったか」を書いてください。"
    "『特に問題なし』のような中身のない文言は禁止です。\n"
    "SELL候補も同じように潰してください（売る必要が本当にあるか）。"
)


def challenge(
    context: dict[str, Any], scout: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, int]]:
    user = (
        CONSTRAINTS_TEXT + "\n"
        + _context_block(context) + "\n\n"
        "## 探索役の提案\n"
        + json.dumps(scout, ensure_ascii=False, indent=2) + "\n\n"
        "提案された候補すべてについて verdict を出してください（漏れなく1件ずつ）。JSONで出力してください。"
    )
    return _call_model(CHALLENGER_SYSTEM, user, CHALLENGER_SCHEMA, "検証役")


# ---------------------------------------------------------------------------
# 4. 執行役: 発注量を決めて理由を残す
# ---------------------------------------------------------------------------

EXECUTOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "orders": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["BUY", "SELL"]},
                    "ticker": {"type": "string"},
                    "amount_usd": {"type": ["number", "null"]},
                    "sell_fraction": {"type": ["number", "null"]},
                    "rule": {"type": "string", "enum": ["new_position", "add", "trim", "exit"]},
                    "reason": {"type": "string"},
                    "review": {"type": ["string", "null"]},
                },
                "required": ["action", "ticker", "amount_usd", "sell_fraction", "rule", "reason", "review"],
                "additionalProperties": False,
            },
        },
        "no_action_reason": {"type": "string"},
    },
    "required": ["orders", "no_action_reason"],
    "additionalProperties": False,
}

EXECUTOR_SYSTEM = (
    "あなたは運用チームの執行役です。検証役の攻撃を生き残った候補について、"
    "**実際にいくら分を売買するか**を決め、注文を出すのが仕事です。\n"
    "生き残った候補を全部買う必要はありません。**注文0件（今日は動かない）も正しい判断です。**\n"
    "BUYはamount_usd（買い付ける金額の目安。整数株に切り捨てられます）を指定します。\n"
    "SELLはsell_fraction（保有株数に対する売却比率。1.0で全部）を指定します。\n"
    "ruleは new_position(新規建て) / add(買い増し) / trim(一部売却) / exit(全部売る) から選びます。\n"
    "**reasonには、その注文を出す時点であなたが何を見て何を考えたかを書いてください。**"
    "これは後から書き換えられず、外れた判断も外れたまま残ります。"
    "『割安だから』のような中身のない文言は禁止です。"
    f"reasonが{config.AI_MIN_REASON_CHARS}文字未満の注文はコード側で拒否されます。\n"
    "rule=exit の注文には review に「この取引の判断は正しかったか」の振り返りを書いてください"
    "（勝ったか負けたかではなく、判断の筋が良かったかを書く）。exit以外ではreviewはnullでかまいません。\n"
    "注文が0件の場合は no_action_reason に「なぜ今日は動かないのか」を書いてください。"
    "注文を出す場合の no_action_reason は空文字でかまいません。"
)


def execute_plan(
    context: dict[str, Any], survivors: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, int]]:
    user = (
        CONSTRAINTS_TEXT + "\n"
        + _context_block(context) + "\n\n"
        "## 検証役の攻撃を生き残った候補（と、潰された候補）\n"
        + json.dumps(survivors, ensure_ascii=False, indent=2) + "\n\n"
        "**潰された候補(killed)は発注できません。** 生き残った候補の中から、"
        "今日出す注文をJSONで出力してください。"
    )
    return _call_model(EXECUTOR_SYSTEM, user, EXECUTOR_SCHEMA, "執行役")


def today_str() -> str:
    return dt.date.today().isoformat()
