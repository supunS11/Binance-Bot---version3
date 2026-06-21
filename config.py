import os
from pathlib import Path

from dotenv import load_dotenv


load_dotenv(Path(__file__).with_name(".env"))


def env_bool(name, default="False"):
    return os.getenv(name, default).strip().lower() == "true"


def env_int(name, default):
    value = os.getenv(name)
    return int(value) if value not in (None, "") else default


def env_float(name, default):
    value = os.getenv(name)
    return float(value) if value not in (None, "") else default


API_KEY = os.getenv("API_KEY")
SECRET_KEY = os.getenv("SECRET_KEY")

SYMBOLS = [
    symbol.strip()
    for symbol in os.getenv("SYMBOLS", "").split(",")
    if symbol.strip()
]

# Long-term mode: 1d trend, 4h confirmation, 1h entry timing.
TREND_TIMEFRAME = os.getenv("TREND_TIMEFRAME", "1d")
CONFIRMATION_TIMEFRAME = os.getenv("CONFIRMATION_TIMEFRAME", "4h")
ENTRY_TIMEFRAME = os.getenv("ENTRY_TIMEFRAME", "1h")
KLINE_LIMIT = env_int("KLINE_LIMIT", 300)
SCAN_SLEEP_SECONDS = env_int("SCAN_SLEEP_SECONDS", 900)
REQUEST_THROTTLE_SECONDS = env_float("REQUEST_THROTTLE_SECONDS", 0.08)
MAX_SCAN_SYMBOLS = env_int("MAX_SCAN_SYMBOLS", 0)
ENTRY_PRICE_RETRIES = env_int("ENTRY_PRICE_RETRIES", 8)
ENTRY_PRICE_RETRY_DELAY_SECONDS = env_float(
    "ENTRY_PRICE_RETRY_DELAY_SECONDS",
    0.25
)
PROTECTION_ORDER_DELAY_SECONDS = env_float(
    "PROTECTION_ORDER_DELAY_SECONDS",
    0.15
)
POST_TRADE_SLEEP_SECONDS = env_float("POST_TRADE_SLEEP_SECONDS", 0.25)

LEVERAGE = env_int("LEVERAGE", 5)
MARGIN_TYPE = os.getenv("MARGIN_TYPE", "CROSSED").upper()
MARGIN_PER_TRADE = env_float("MARGIN_PER_TRADE", 5)

STATIC_TP_ENABLED = env_bool("STATIC_TP_ENABLED", "True")
STATIC_TP_ROI = env_float("STATIC_TP_ROI", env_float("ROI_PERCENT_TP", 6))
ROI_PERCENT_TP = env_float("ROI_PERCENT_TP", STATIC_TP_ROI)

# Default long-term behavior is no exchange SL. The adverse-zone support or
# resistance check is used for trade quality, not as a mandatory stop order.
SL_ENABLED = env_bool("SL_ENABLED", "False")
MAX_SL_ROI = env_float("MAX_SL_ROI", 50)

LONG_TERM_SIGNAL_THRESHOLD = env_float("LONG_TERM_SIGNAL_THRESHOLD", 72)
LONG_TERM_MIN_SIGNAL_EDGE = env_float("LONG_TERM_MIN_SIGNAL_EDGE", 5)
LONG_TERM_MAX_ADVERSE_ROI = env_float("LONG_TERM_MAX_ADVERSE_ROI", 50)
LONG_TERM_SR_LOOKBACK = env_int("LONG_TERM_SR_LOOKBACK", 160)
LONG_TERM_SR_MIN_TOUCHES = env_int("LONG_TERM_SR_MIN_TOUCHES", 2)
LONG_TERM_SR_MIN_SCORE = env_float("LONG_TERM_SR_MIN_SCORE", 2.5)
LONG_TERM_SR_TOLERANCE_PCT = env_float("LONG_TERM_SR_TOLERANCE_PCT", 1.0)
LONG_TERM_SR_ATR_TOLERANCE = env_float("LONG_TERM_SR_ATR_TOLERANCE", 0.75)
LONG_TERM_ENTRY_MAX_EMA_DISTANCE_PCT = env_float(
    "LONG_TERM_ENTRY_MAX_EMA_DISTANCE_PCT",
    6.0
)
LONG_TERM_MIN_ADX = env_float("LONG_TERM_MIN_ADX", 14)
LONG_TERM_BTC_CORR_THRESHOLD = env_float("LONG_TERM_BTC_CORR_THRESHOLD", 0.65)

MAX_TOTAL_POSITIONS = (
    int(os.getenv("MAX_TOTAL_POSITIONS"))
    if os.getenv("MAX_TOTAL_POSITIONS")
    else None
)

MAX_BUY_POSITIONS = (
    int(os.getenv("MAX_BUY_POSITIONS"))
    if os.getenv("MAX_BUY_POSITIONS")
    else None
)

MAX_SELL_POSITIONS = (
    int(os.getenv("MAX_SELL_POSITIONS"))
    if os.getenv("MAX_SELL_POSITIONS")
    else None
)

TESTNET = env_bool("TESTNET", "False")
