"""vs-sp500: moomooのINDUSTRY plate名から11種のSPDRセクターETFへの分類表（2026-10-07追加）。

スワップ売却機能（SPEC_RSI30.md「2026-10-07改訂」参照）のセクターTier計算で使う。
moomooのget_owner_plateが返すINDUSTRY分類（例: "Oil & Gas Integrated"・"Banks - Diversified"）を
キーワードの部分一致でSPDRセクターETF（XLK/XLF/XLV/XLE/XLI/XLY/XLP/XLU/XLB/XLRE/XLC）へ丸める。
一致しない・INDUSTRY分類が取得できない銘柄はNone（呼び出し側はセクターTierを0として扱う＝
大将「q1)2」セクター分類はmoomoo由来、不明銘柄の扱いは「Unknown sector → 0」仕様どおり）。

この関数群は純粋関数のみで構成する（moomoo呼び出し・ファイルI/Oを含まない）。
"""
from __future__ import annotations

# (キーワード, ETF) の優先順位リスト。大文字小文字を無視した部分一致で最初に当たったものを採用する。
# REIT/Real Estateを最優先にしているのは、"Financial"等の広いキーワードに先に当たって
# 不動産銘柄金融セクターへ誤分類されるのを防ぐため。
_KEYWORD_RULES: tuple[tuple[str, str], ...] = (
    # Real Estate (XLRE)
    ("reit", "XLRE"), ("real estate", "XLRE"),
    # Energy (XLE)
    ("oil & gas", "XLE"), ("oil", "XLE"), ("coal", "XLE"), ("uranium", "XLE"), ("energy", "XLE"),
    # Utilities (XLU)
    ("utilities", "XLU"), ("utility", "XLU"),
    # Materials (XLB)
    ("chemicals", "XLB"), ("steel", "XLB"), ("mining", "XLB"), ("metals", "XLB"),
    ("paper", "XLB"), ("packaging", "XLB"), ("building materials", "XLB"), ("agricultural inputs", "XLB"),
    # Health Care (XLV)
    ("biotechnology", "XLV"), ("pharmaceutical", "XLV"), ("drug", "XLV"), ("medical", "XLV"),
    ("health", "XLV"), ("hospital", "XLV"), ("diagnostics", "XLV"),
    # Financial (XLF)
    ("bank", "XLF"), ("insurance", "XLF"), ("asset management", "XLF"), ("capital markets", "XLF"),
    ("credit services", "XLF"), ("financial", "XLF"), ("exchange", "XLF"), ("mortgage", "XLF"),
    # Communication Services (XLC)
    ("telecom", "XLC"), ("broadcasting", "XLC"), ("entertainment", "XLC"), ("publishing", "XLC"),
    ("internet content", "XLC"), ("social media", "XLC"), ("advertising", "XLC"),
    # Technology (XLK)
    ("semiconductor", "XLK"), ("software", "XLK"), ("hardware", "XLK"), ("electronics", "XLK"),
    ("information technology", "XLK"), ("it services", "XLK"), ("computer", "XLK"),
    # Consumer Staples (XLP)
    ("beverages", "XLP"), ("food", "XLP"), ("household", "XLP"), ("personal products", "XLP"),
    ("tobacco", "XLP"), ("grocery", "XLP"), ("discount stores", "XLP"), ("agricultural farm", "XLP"),
    # Industrials (XLI)
    ("aerospace", "XLI"), ("defense", "XLI"), ("airlines", "XLI"), ("railroads", "XLI"),
    ("trucking", "XLI"), ("industrial", "XLI"), ("machinery", "XLI"), ("waste management", "XLI"),
    ("construction", "XLI"), ("engineering", "XLI"), ("staffing", "XLI"), ("conglomerates", "XLI"),
    ("marine shipping", "XLI"), ("infrastructure", "XLI"),
    # Consumer Discretionary (XLY) — 小売・自動車・娯楽系はキーワードが広いため最後にまとめて判定
    ("auto", "XLY"), ("apparel", "XLY"), ("retail", "XLY"), ("restaurants", "XLY"),
    ("leisure", "XLY"), ("resorts", "XLY"), ("travel", "XLY"), ("luxury", "XLY"), ("furnishings", "XLY"),
    ("homebuilding", "XLY"), ("specialty", "XLY"), ("department stores", "XLY"),
)


def classify_industry_labels(industry_labels: list[str]) -> str | None:
    """1銘柄分のINDUSTRY plate名リストから、最初にキーワードへ一致したSPDRセクターETFを返す。

    どの銘柄のラベルにも一致しなければNone（スコアは呼び出し側で0として扱われる）。
    """
    for label in industry_labels:
        if not label:
            continue
        lower = label.lower()
        for keyword, etf in _KEYWORD_RULES:
            if keyword in lower:
                return etf
    return None
