from binance.client import Client
from binance.enums import *

import pandas as pd
import time
import numpy as np

import config
from indicators import apply_indicators
from logger import log_info, log_warning, log_error


client = Client(config.API_KEY, config.SECRET_KEY)
_exchange_info_cache = None
_last_kline_request_at = 0.0

# =========================
# SYNC TIME
# =========================
server_time = client.get_server_time()
client.timestamp_offset = server_time['serverTime'] - int(time.time() * 1000)


def _throttle_kline_request():
    global _last_kline_request_at

    delay = getattr(config, "REQUEST_THROTTLE_SECONDS", 0)

    if delay <= 0:
        return

    elapsed = time.time() - _last_kline_request_at

    if elapsed < delay:
        time.sleep(delay - elapsed)

    _last_kline_request_at = time.time()


def get_exchange_info():
    global _exchange_info_cache

    if _exchange_info_cache is None:
        _exchange_info_cache = client.futures_exchange_info()

    return _exchange_info_cache


def get_supported_symbols():

    try:
        symbols = set()

        for item in get_exchange_info().get("symbols", []):
            if item.get("status") != "TRADING":
                continue

            if item.get("contractType") != "PERPETUAL":
                continue

            symbols.add(item["symbol"])

        return symbols

    except Exception as e:
        log_error(f"supported symbols error: {e}")
        return set()


# =========================
# MARGIN TYPE
# =========================
def set_margin_type(symbol):

    try:
        client.futures_change_margin_type(
            symbol=symbol,
            marginType=config.MARGIN_TYPE
        )

        log_info(f"{symbol} Margin: {config.MARGIN_TYPE}")
        return True

    except Exception as e:
        if "No need to change margin type" not in str(e):
            log_warning(str(e))
            return False

        log_info(f"{symbol} Margin already {config.MARGIN_TYPE}")
        return True


# =========================
# LEVERAGE
# =========================
def setup_leverage(symbol):

    try:

        response = client.futures_change_leverage(
            symbol=symbol,
            leverage=config.LEVERAGE
        )

        actual = int(response['leverage'])

        if actual != config.LEVERAGE:
            log_warning(f"{symbol} leverage mismatch")
            return False

        log_info(f"{symbol} leverage set: {actual}x")
        return True

    except Exception as e:
        log_error(f"{symbol} leverage error: {e}")
        return False


# =========================
# BALANCE
# =========================
def get_balance():

    balances = client.futures_account_balance()

    for b in balances:
        if b['asset'] == 'USDT':
            return float(b['balance'])

    return 0


def get_margin_balance():
    return float(client.futures_account()['totalMarginBalance'])


def get_unrealized_pnl():
    return float(client.futures_account()['totalUnrealizedProfit'])


# =========================
# KLINES
# =========================
def get_klines(symbol, interval, limit=None):

    try:
        limit = limit if limit is not None else config.KLINE_LIMIT
        _throttle_kline_request()

        klines = client.futures_klines(
            symbol=symbol,
            interval=interval,
            limit=limit
        )

        df = pd.DataFrame(klines, columns=[
            'time', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'qav', 'trades', 'tbbav', 'tbqav', 'ignore'
        ])

        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = df[col].astype(float)

        return df

    except Exception as e:
        log_error(f"{symbol} klines error: {e}")
        return None


# =========================
# POSITION CHECKS
# =========================
def has_open_position(symbol):

    try:
        positions = client.futures_position_information(symbol=symbol)

        for p in positions:
            if float(p['positionAmt']) != 0:
                return True

        return False

    except Exception as e:
        log_error(str(e))
        return False


def is_position_closed(symbol):

    try:
        positions = client.futures_position_information(symbol=symbol)

        for p in positions:
            if abs(float(p['positionAmt'])) > 0:
                return False

        return True

    except Exception as e:
        log_error(f"{symbol} position check error: {e}")
        return False


def get_open_positions():

    try:
        positions = client.futures_position_information()
        open_positions = {}

        for p in positions:
            amount = float(p["positionAmt"])

            if amount != 0:
                open_positions[p["symbol"]] = amount

        return open_positions

    except Exception as e:
        log_error(f"open positions error: {e}")
        return None


def get_open_position_counts(open_positions=None):

    try:

        if open_positions is None:
            open_positions = get_open_positions()

        if open_positions is None:
            return {"total": 0, "buy": 0, "sell": 0}

        total = buy = sell = 0

        for amt in open_positions.values():

            if amt == 0:
                continue

            total += 1

            if amt > 0:
                buy += 1
            else:
                sell += 1

        return {
            "total": total,
            "buy": buy,
            "sell": sell
        }

    except Exception as e:
        log_error(f"position count error: {e}")
        return {"total": 0, "buy": 0, "sell": 0}


# =========================
# PRECISION
# =========================
def get_symbol_precision(symbol):

    info = get_exchange_info()

    for s in info['symbols']:
        if s['symbol'] == symbol:
            return s['quantityPrecision']

    return 3


def get_price_precision(symbol):

    info = get_exchange_info()

    for s in info['symbols']:
        if s['symbol'] == symbol:
            return int(s['pricePrecision'])

    return 4


# =========================
# ENTRY PRICE
# =========================
def get_entry_price(symbol, order=None):

    if order:
        avg_price = float(order.get("avgPrice", 0) or 0)

        if avg_price <= 0:
            executed_qty = float(order.get("executedQty", 0) or 0)
            cum_quote = float(order.get("cumQuote", 0) or 0)

            if executed_qty > 0 and cum_quote > 0:
                avg_price = cum_quote / executed_qty

        if avg_price > 0:
            return avg_price

    last_error = None

    for attempt in range(config.ENTRY_PRICE_RETRIES):
        try:
            positions = client.futures_position_information(symbol=symbol)
            entry_price = abs(float(positions[0]["entryPrice"]))

            if entry_price > 0:
                return entry_price

        except Exception as e:
            last_error = e

        if attempt < config.ENTRY_PRICE_RETRIES - 1:
            time.sleep(config.ENTRY_PRICE_RETRY_DELAY_SECONDS)

    if last_error:
        log_warning(f"{symbol} entry price polling error: {last_error}")

    return 0


# =========================
# MARKET ORDER
# =========================
def place_market_order(symbol, side, quantity):

    try:

        order = client.futures_create_order(
            symbol=symbol,
            side=side,
            type=FUTURE_ORDER_TYPE_MARKET,
            quantity=quantity,
            newOrderRespType="RESULT"
        )

        log_info(f"{symbol} MARKET ORDER: {side}")
        return order

    except Exception as e:
        log_error(f"{symbol} order error: {e}")
        return None


# =========================
# STRUCTURE SL (REQUIRED BY MAIN + STRATEGY)
# =========================
def get_structure_stop_loss(df, side):

    try:

        atr = df['atr'].iloc[-1]

        if side == SIDE_BUY:

            swing_low = df['low'].iloc[-10:-1].min()
            return swing_low - (atr * 0.5)

        else:

            swing_high = df['high'].iloc[-10:-1].max()
            return swing_high + (atr * 0.5)

    except Exception as e:
        log_error(f"SL error: {e}")
        return None


# =========================
# TP/SL EXECUTION (CLEAN VERSION)
# =========================
def place_tp_sl(symbol, side, entry_price, quantity, confirm_df):

    try:
        precision = get_price_precision(symbol)

        market_price = float(
            client.futures_mark_price(symbol=symbol)['markPrice']
        )

        # ================= BUY =================
        tp_roi = (
            config.STATIC_TP_ROI
            if getattr(config, "STATIC_TP_ENABLED", True)
            else config.ROI_PERCENT_TP
        )

        if side == SIDE_BUY:

            tp_price = round(
                entry_price * (1 + (tp_roi / config.LEVERAGE) / 100),
                precision
            )

            sl_price = None

            if config.SL_ENABLED:
                sl_price = round(
                    get_structure_stop_loss(confirm_df, SIDE_BUY),
                    precision
                )

            close_side = SIDE_SELL

        # ================= SELL =================
        else:

            tp_price = round(
                entry_price * (1 - (tp_roi / config.LEVERAGE) / 100),
                precision
            )

            sl_price = None

            if config.SL_ENABLED:
                sl_price = round(
                    get_structure_stop_loss(confirm_df, SIDE_SELL),
                    precision
                )

            close_side = SIDE_BUY

        # ================= VALIDATION ONLY =================
        if side == SIDE_BUY:
            if tp_price <= market_price:
                return False

            if config.SL_ENABLED and sl_price >= market_price:
                return False
        else:
            if tp_price >= market_price:
                return False

            if config.SL_ENABLED and sl_price <= market_price:
                return False

        log_info(
            f"{symbol}\nENTRY: {entry_price}\nTP: {tp_price}\n"
            f"SL: {sl_price if config.SL_ENABLED else 'DISABLED'}"
        )

        # TAKE PROFIT
        client.futures_create_order(
            symbol=symbol,
            side=close_side,
            type="TAKE_PROFIT_MARKET",
            stopPrice=tp_price,
            closePosition=True,
            workingType="MARK_PRICE",
            priceProtect=True
        )

        if config.SL_ENABLED:
            time.sleep(config.PROTECTION_ORDER_DELAY_SECONDS)

            # STOP LOSS
            client.futures_create_order(
                symbol=symbol,
                side=close_side,
                type="STOP_MARKET",
                stopPrice=sl_price,
                closePosition=True,
                workingType="MARK_PRICE",
                priceProtect=True
            )
        else:
            log_warning(f"{symbol} SL DISABLED | CROSS-MARGIN LONG-TERM MODE")

        log_info(f"{symbol} TP CREATED")
        return True

    except Exception as e:
        log_error(f"{symbol} TP/SL error: {e}")
        return False


# =========================
# BTC CORRELATION
# =========================
def get_btc_correlation(symbol):

    try:

        if symbol == "BTCUSDT":
            return 1.0

        coin_df = get_klines(symbol, config.TREND_TIMEFRAME, 100)
        btc_df = get_klines("BTCUSDT", config.TREND_TIMEFRAME, 100)

        if coin_df is None or btc_df is None:
            return 0

        coin_ret = coin_df['close'].pct_change().dropna()
        btc_ret = btc_df['close'].pct_change().dropna()

        return round(float(np.corrcoef(coin_ret, btc_ret)[0, 1]), 2)

    except Exception as e:
        log_error(f"{symbol} corr error: {e}")
        return 0


# =========================
# BTC TREND
# =========================
def get_btc_trend():

    try:

        btc_df = get_klines("BTCUSDT", config.TREND_TIMEFRAME)
        btc_df = apply_indicators(btc_df)

        btc = btc_df.iloc[-2]

        if btc['ema50'] > btc['ema200']:
            return "BULLISH"
        elif btc['ema50'] < btc['ema200']:
            return "BEARISH"

        return "NEUTRAL"

    except Exception as e:
        log_error(f"BTC trend error: {e}")
        return None


# =========================
# RELATIVE STRENGTH
# =========================
def get_relative_strength(symbol):

    try:

        if symbol == "BTCUSDT":
            return 0

        coin = get_klines(symbol, config.TREND_TIMEFRAME, 50)
        btc = get_klines("BTCUSDT", config.TREND_TIMEFRAME, 50)

        if coin is None or btc is None:
            return 0

        coin_r = (coin['close'].iloc[-1] - coin['close'].iloc[-10]) / coin['close'].iloc[-10] * 100
        btc_r = (btc['close'].iloc[-1] - btc['close'].iloc[-10]) / btc['close'].iloc[-10] * 100

        return round(coin_r - btc_r, 2)

    except Exception as e:
        log_error(f"{symbol} RS error: {e}")
        return 0
    
def validate_min_notional(symbol, quantity, price):

    try:

        notional = quantity * price

        # Binance futures minimum notional (safe default buffer)
        MIN_NOTIONAL = 5.0

        if notional < MIN_NOTIONAL:
            return False, notional

        return True, notional

    except Exception:
        return False, 0
