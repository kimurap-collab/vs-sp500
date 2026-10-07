"""vs-sp500: 日本株RSI枠のセクター分類表（yfinanceのsector/industry → TOPIX-17シリーズETF）。

2026-10-07追加・スワップ売却機能(JP枠)。TOPIX-17シリーズETF（NEXT FUNDS。銘柄コード1617〜1633。
yfinanceでは"{code}.T"）への分類に使う。

マッピング（_IND・_SECTOR_FALLBACK・_TICKER_OVERRIDES）は参照実装
（/private/tmp/.../scratchpad/backtest5y/sectormap.py のIND・JP_SEC_FALLBACK・HELD17を
そのまま流用。TSE33業種→TOPIX-17への対応表と、個別検証済み32銘柄の明示的な上書き）を再利用している。
US_MAP/us_sector相当（米国枠のSPDR分類）はこのモジュールには含めない（JP専用のため）。

この関数群は純粋関数のみで構成する（yfinance呼び出し・ファイルI/Oを含まない）。
"""
from __future__ import annotations

# industry名 → TOPIX-17コード
_IND: dict[str, str] = {}


def _add(code: str, *names: str) -> None:
    for name in names:
        _IND[name] = code


_add("1617", "Packaged Foods", "Beverages - Non-Alcoholic", "Beverages - Brewers",
     "Beverages - Wineries & Distilleries", "Confectioners", "Farm Products", "Tobacco")
_add("1618", "Oil & Gas Integrated", "Oil & Gas E&P", "Oil & Gas Refining & Marketing",
     "Oil & Gas Equipment & Services", "Thermal Coal", "Uranium", "Oil & Gas Midstream")
_add("1619", "Engineering & Construction", "Building Materials", "Residential Construction",
     "Infrastructure Operations")
_add("1620", "Specialty Chemicals", "Chemicals", "Agricultural Inputs", "Paper & Paper Products",
     "Packaging & Containers", "Textile Manufacturing", "Lumber & Wood Production",
     "Household & Personal Products", "Apparel Manufacturing")
_add("1621", "Drug Manufacturers - General", "Drug Manufacturers - Specialty & Generic", "Biotechnology")
_add("1622", "Auto Manufacturers", "Auto Parts", "Recreational Vehicles")
_add("1623", "Steel", "Aluminum", "Copper", "Other Industrial Metals & Mining", "Metal Fabrication",
     "Gold", "Silver", "Other Precious Metals & Mining")
_add("1624", "Specialty Industrial Machinery", "Farm & Heavy Construction Machinery", "Tools & Accessories",
     "Pollution & Treatment Controls", "Building Products & Equipment", "Aerospace & Defense")
_add("1625", "Electronic Components", "Semiconductors", "Semiconductor Equipment & Materials",
     "Scientific & Technical Instruments", "Consumer Electronics", "Computer Hardware",
     "Communication Equipment", "Electrical Equipment & Parts", "Medical Instruments & Supplies",
     "Medical Devices", "Diagnostics & Research", "Business Equipment & Supplies", "Solar")
_add("1626", "Software - Application", "Software - Infrastructure", "Information Technology Services",
     "Telecom Services", "Internet Content & Information", "Advertising Agencies", "Entertainment",
     "Electronic Gaming & Multimedia", "Publishing", "Broadcasting", "Staffing & Employment Services",
     "Specialty Business Services", "Consulting Services", "Education & Training Services",
     "Personal Services", "Leisure", "Medical Care Facilities", "Health Information Services",
     "Security & Protection Services", "Waste Management", "Lodging", "Resorts & Casinos", "Gambling",
     "Travel Services", "Furnishings, Fixtures & Appliances", "Footwear & Accessories", "Luxury Goods")
_add("1627", "Utilities - Regulated Electric", "Utilities - Regulated Gas", "Utilities - Renewable",
     "Utilities - Independent Power Producers", "Utilities - Diversified", "Utilities - Regulated Water")
_add("1628", "Railroads", "Trucking", "Marine Shipping", "Airlines", "Integrated Freight & Logistics",
     "Airports & Air Services")
_add("1629", "Conglomerates", "Industrial Distribution", "Food Distribution",
     "Electronics & Computer Distribution", "Medical Distribution")
_add("1630", "Department Stores", "Discount Stores", "Grocery Stores", "Specialty Retail",
     "Home Improvement Retail", "Apparel Retail", "Internet Retail", "Pharmaceutical Retailers",
     "Restaurants", "Auto & Truck Dealerships")
_add("1631", "Banks - Regional", "Banks - Diversified")
_add("1632", "Insurance - Life", "Insurance - Diversified", "Insurance - Property & Casualty",
     "Capital Markets", "Credit Services", "Asset Management", "Financial Conglomerates",
     "Financial Data & Stock Exchanges", "Insurance Brokers", "Mortgage Finance",
     "Rental & Leasing Services", "Insurance - Reinsurance", "Insurance - Specialty")
_add("1633", "Real Estate Services", "Real Estate - Development", "Real Estate - Diversified")

# sector名 → TOPIX-17コード（industryで一致しなかった場合のフォールバック）
_SECTOR_FALLBACK: dict[str, str] = {
    "Technology": "1625", "Industrials": "1624", "Consumer Cyclical": "1630",
    "Consumer Defensive": "1617", "Basic Materials": "1620", "Healthcare": "1621",
    "Financial Services": "1632", "Communication Services": "1626", "Utilities": "1627",
    "Energy": "1618", "Real Estate": "1633",
}

# 特定銘柄の明示的な上書き（参照実装のHELD17をそのまま流用。TSE分類で個別検証済みの銘柄）
_TICKER_OVERRIDES: dict[str, str] = {
    "6367": "1624", "6013": "1624", "6235": "1624", "6479": "1625", "5334": "1619",
    "3905": "1626", "7966": "1626", "268A": "1625", "4980": "1620", "4966": "1620",
    "4047": "1620", "7532": "1630", "1979": "1619", "6707": "1625", "6141": "1624",
    "9983": "1630", "6361": "1624", "7944": "1626", "4368": "1620", "3099": "1630",
    "3465": "1633", "6841": "1625", "4099": "1620", "6506": "1625", "9065": "1628",
    "5332": "1619", "2264": "1617", "7220": "1622", "9412": "1626", "9143": "1628",
    "6845": "1625", "6101": "1624",
}


def classify_info(info: dict | None, ticker: str | None = None) -> str | None:
    """1銘柄分のyfinance .info（sector/industryキーを含む）からTOPIX-17コードを返す。

    判定順序: (1) ticker が_TICKER_OVERRIDESに載っていればinfoの内容に関わらずそちらを優先する。
    (2) industryが_IND辞書に一致すればそれを採用する。(3) 一致しなければsectorで
    _SECTOR_FALLBACKを引く。(1)〜(3)いずれも一致しない・infoが無ければNone
    （呼び出し側はTierを0として扱う＝「Unknown sector → 0」仕様どおり）。
    """
    if ticker is not None and ticker in _TICKER_OVERRIDES:
        return _TICKER_OVERRIDES[ticker]
    info = info or {}
    industry = info.get("industry")
    if industry in _IND:
        return _IND[industry]
    sector = info.get("sector")
    return _SECTOR_FALLBACK.get(sector)
