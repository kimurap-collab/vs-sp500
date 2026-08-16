"""vs-sp500: 定数定義と.env読み込み。"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# --- パス ---
BASE_DIR = Path(__file__).resolve().parent
CHARTER_PATH = BASE_DIR / "charter.md"
LEDGER_DIR = BASE_DIR / "ledger"
PORTFOLIO_PATH = LEDGER_DIR / "portfolio.json"
TRADES_CSV_PATH = LEDGER_DIR / "trades.csv"
HISTORY_CSV_PATH = LEDGER_DIR / "history.csv"
DATA_JSON_PATH = BASE_DIR / "data.json"
PENDING_REPORT_PATH = BASE_DIR / "pending_report.txt"  # 夜間実行の報告を朝まで貯める
LOG_DIR = BASE_DIR / "logs"

# --- .env読み込み ---
load_dotenv(BASE_DIR / ".env")

CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# --- 投資ユニバース（ホワイトリスト。charter.mdと一致させること） ---
WHITELIST: dict[str, dict[str, str]] = {
    "VOO": {"currency": "USD", "name": "S&P500 ETF", "type": "etf"},
    "QQQ": {"currency": "USD", "name": "ナスダック100 ETF", "type": "etf"},
    "GLD": {"currency": "USD", "name": "金 ETF", "type": "etf"},
    "TLT": {"currency": "USD", "name": "米国長期債 ETF", "type": "etf"},
    "IEF": {"currency": "USD", "name": "米国中期債 ETF", "type": "etf"},
    "XLV": {"currency": "USD", "name": "米国ヘルスケアセクター ETF", "type": "etf"},
    "XLE": {"currency": "USD", "name": "米国エネルギーセクター ETF", "type": "etf"},
    "EWJ": {"currency": "USD", "name": "MSCI日本株 ETF", "type": "etf"},
    # 個別株を足す時は "type": "stock" を指定する（憲章の追加ルールが自動適用される）
}


def is_stock(ticker: str) -> bool:
    """個別株か（ETFでないか）。憲章の個別株ルール適用判定に使う。"""
    return WHITELIST.get(ticker, {}).get("type") == "stock"
BENCHMARK_TICKER = "VOO"
FX_TICKER = "USDJPY=X"  # 管理画面の参考表示用。評価計算には使わない（v1.4でドル建てに移行）
BASE_CURRENCY = "USD"

# --- 初期資金 ---
INITIAL_CAPITAL_USD = 63343.06  # 10,000,000円 ÷ 開始日レート157.87

# --- moomoo（v1.4: 売買・実費はmoomoo仮想口座に移行） ---
MOOMOO_HOST = "127.0.0.1"
MOOMOO_PORT = 11111
MOOMOO_ACC_ID = 5338087

# --- ガードレール（charter.md準拠） ---
MAX_TICKER_WEIGHT = 0.30
MAX_VOO_WEIGHT = 0.65
# 個別株の追加ガードレール（憲章v1.2）
MAX_STOCK_WEIGHT = 0.05          # 個別株1銘柄の上限
MAX_STOCK_TOTAL_WEIGHT = 0.20    # 個別株合計の上限
STOCK_STOP_LOSS = -0.20          # 平均取得単価比でこれを下回ったら全売却
MIN_CASH_RATIO = 0.02
MAX_DAILY_TRADES = 10
NON_TARGET_TRADE_DAILY_CAP_OF_NAV = 0.10  # 押し目買い等、1日あたり評価額の10%まで
DIP_BUY_MAX_OF_CASH = 0.5  # 押し目買いは現金の半分まで
TARGET_WEIGHT_TOLERANCE = 0.0001  # 現金比率・集中規制の許容誤差
TARGET_OVERSHOOT_TOLERANCE = 0.01  # ターゲット超過判定の許容誤差（1ポイント。手数料による分母減少で誤検知しないため）

# --- 発動条件の閾値（charter.md準拠） ---
REBALANCE_DEVIATION_THRESHOLD = 0.05  # ±5ポイント
TREND_DEFENSE_STREAK_DAYS = 3
DIP_BUY_DROP_FROM_HIGH = 0.12  # 52週高値から12%以上下落
DIP_BUY_RSI_THRESHOLD = 30

# --- 市場データ取得のリトライ（yfinanceの一時的な応答不良対策） ---
MARKET_FETCH_RETRIES = 3
MARKET_FETCH_BACKOFF_SEC = 3.0

# --- Sonnet（番兵） ---
ANTHROPIC_MODEL = "claude-sonnet-5"
ANTHROPIC_MAX_TOKENS = 2000
ANTHROPIC_EFFORT = "low"

# --- Telegram / GitHub Pages ---
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
GITHUB_PAGES_URL = "https://kimurap-collab.github.io/vs-sp500/"

# --- data.json用の取引履歴保持件数 ---
DATA_JSON_TRADES_LIMIT = 50
