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
LOG_DIR = BASE_DIR / "logs"

# --- .env読み込み ---
load_dotenv(BASE_DIR / ".env")

CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# --- 投資ユニバース（ホワイトリスト。charter.mdと一致させること） ---
WHITELIST: dict[str, dict[str, str]] = {
    "VOO": {"currency": "USD", "name": "S&P500 ETF"},
    "QQQ": {"currency": "USD", "name": "ナスダック100 ETF"},
    "GLD": {"currency": "USD", "name": "金 ETF"},
    "TLT": {"currency": "USD", "name": "米国長期債 ETF"},
    "IEF": {"currency": "USD", "name": "米国中期債 ETF"},
    "XLV": {"currency": "USD", "name": "米国ヘルスケアセクター ETF"},
    "XLE": {"currency": "USD", "name": "米国エネルギーセクター ETF"},
    "1306.T": {"currency": "JPY", "name": "TOPIX連動 ETF"},
}
BENCHMARK_TICKER = "VOO"
FX_TICKER = "USDJPY=X"

# --- 初期資金 ---
INITIAL_CAPITAL_JPY = 10_000_000

# --- コストモデル（charter.md準拠） ---
US_ETF_FEE_RATE = 0.00495
US_ETF_FEE_CAP_USD = 22.0
JP_ETF_FEE_RATE = 0.0
FX_SPREAD_JPY_PER_USD = 0.10  # 片道。買いは仲値+0.10円、売りは仲値-0.10円

# --- ガードレール（charter.md準拠） ---
MAX_TICKER_WEIGHT = 0.30
MAX_VOO_WEIGHT = 0.65
MIN_CASH_RATIO = 0.02
MAX_DAILY_TRADES = 10
NON_TARGET_TRADE_DAILY_CAP_OF_NAV = 0.10  # 押し目買い等、1日あたり評価額の10%まで
DIP_BUY_MAX_OF_CASH = 0.5  # 押し目買いは現金の半分まで
TARGET_WEIGHT_TOLERANCE = 0.0001  # ターゲット超過判定の許容誤差

# --- 発動条件の閾値（charter.md準拠） ---
REBALANCE_DEVIATION_THRESHOLD = 0.05  # ±5ポイント
TREND_DEFENSE_STREAK_DAYS = 3
DIP_BUY_DROP_FROM_HIGH = 0.12  # 52週高値から12%以上下落
DIP_BUY_RSI_THRESHOLD = 30

# --- Sonnet（番兵） ---
ANTHROPIC_MODEL = "claude-sonnet-5"
ANTHROPIC_MAX_TOKENS = 2000
ANTHROPIC_EFFORT = "low"

# --- Telegram / GitHub Pages ---
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
GITHUB_PAGES_URL = "https://kimurap-collab.github.io/vs-sp500/"

# --- data.json用の取引履歴保持件数 ---
DATA_JSON_TRADES_LIMIT = 50
