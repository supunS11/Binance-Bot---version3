import time
from datetime import datetime

import config

from binance.enums import *

from exchange import (
    get_klines,
    get_balance,
    place_market_order,
    place_tp_sl,
    has_open_position,
    get_open_position_counts,
    is_position_closed,
    setup_leverage,
    get_entry_price,
    get_margin_balance,
    get_unrealized_pnl,
    get_btc_trend,
    get_btc_correlation,
    get_relative_strength
)

from indicators import apply_indicators
from strategy import check_signal
from risk_management import calculate_position_size
from logger import log_info, log_warning, log_error


# STORE TRADE TIMES
trade_times = {}


def run_bot():

    log_info("BOT STARTED")

    while True:

        try:

            for symbol in config.SYMBOLS:

                try:

                    log_info(f"Checking {symbol}")

                    # =========================
                    # CHECK CLOSED POSITIONS
                    # =========================
                    if symbol in trade_times:

                        if is_position_closed(symbol):

                            exit_time = datetime.now()
                            entry_time = trade_times[symbol]['entry_time']

                            duration = exit_time - entry_time

                            log_info(
                                f"*** {symbol} TRADE CLOSED *** | "
                                f"ENTRY: {entry_time} | "
                                f"EXIT: {exit_time} | "
                                f"DURATION: {duration}"
                            )

                            del trade_times[symbol]

                    # =========================
                    # PREVENT DUPLICATE POSITIONS
                    # =========================
                    if has_open_position(symbol):

                        log_warning(f"{symbol} already has open position")
                        continue

                    # =========================
                    # GET DATA
                    # =========================
                    trend_df = get_klines(symbol, config.TREND_TIMEFRAME)
                    confirm_df = get_klines(symbol, config.CONFIRMATION_TIMEFRAME)
                    entry_df = get_klines(symbol, config.ENTRY_TIMEFRAME)

                    if trend_df is None or confirm_df is None or entry_df is None:
                        log_warning(f"{symbol} dataframe empty")
                        continue

                    if len(trend_df) < 250 or len(confirm_df) < 250 or len(entry_df) < 250:
                        log_warning(f"{symbol} insufficient candle data")
                        continue

                    # =========================
                    # INDICATORS
                    # =========================
                    trend_df = apply_indicators(trend_df)
                    confirm_df = apply_indicators(confirm_df)
                    entry_df = apply_indicators(entry_df)

                    if trend_df is None or confirm_df is None or entry_df is None:
                        log_warning(f"{symbol} indicator failure")
                        continue

                    if len(trend_df) < 2 or len(confirm_df) < 2 or len(entry_df) < 2:
                        log_warning(f"{symbol} insufficient indicator data")
                        continue

                    # =========================
                    # BTC CORRELATION
                    # =========================
                    btc_trend = get_btc_trend()

                    btc_corr = get_btc_correlation(
                        symbol
                    )

                    log_info(
                        f"{symbol} BTC CORR: "
                        f"{btc_corr}"
                    )

                    log_info(
                        f"BTC TREND: "
                        f"{btc_trend}"
                    )

                    rs = get_relative_strength(symbol)

                    log_info(
                        f"{symbol} RELATIVE STRENGTH: {rs}%"
                    )

                    # =========================
                    # SIGNAL
                    # =========================
                    signal = check_signal(trend_df, confirm_df, entry_df, btc_trend, btc_corr, rs)

                    if not signal:
                        log_warning(f"{symbol} NO SIGNAL FOUND")
                        time.sleep(2)
                        continue

                    log_info(f"{symbol} {signal} SIGNAL DETECTED")

                    # =========================
                    # POSITION LIMITS
                    # =========================
                    counts = get_open_position_counts()

                    if config.MAX_TOTAL_POSITIONS and counts['total'] >= config.MAX_TOTAL_POSITIONS:
                        log_warning("MAX TOTAL POSITIONS REACHED")
                        continue

                    if signal == "BUY":
                        if config.MAX_BUY_POSITIONS and counts['buy'] >= config.MAX_BUY_POSITIONS:
                            log_warning("MAX BUY POSITIONS REACHED")
                            continue

                    if signal == "SELL":
                        if config.MAX_SELL_POSITIONS and counts['sell'] >= config.MAX_SELL_POSITIONS:
                            log_warning("MAX SELL POSITIONS REACHED")
                            continue

                    # =========================
                    # ACCOUNT DATA
                    # =========================
                    balance = get_balance()
                    totalMarginBalance = get_margin_balance()
                    totalUnrealizedProfit = get_unrealized_pnl()

                    current_price = entry_df['close'].iloc[-2]

                    # =========================
                    # POSITION SIZE
                    # =========================
                    quantity = calculate_position_size(balance, current_price, symbol)

                    if quantity <= 0:
                        log_warning(f"{symbol} invalid quantity")
                        continue

                    log_info(f"{symbol} Quantity: {quantity}")

                    # =========================
                    # LEVERAGE
                    # =========================
                    if not setup_leverage(symbol):
                        log_warning(f"{symbol} leverage setup failed")
                        continue

                    # =========================
                    # PLACE ORDER
                    # =========================
                    if signal == "BUY":

                        place_market_order(symbol, SIDE_BUY, quantity)
                        time.sleep(2)

                        entry_price = get_entry_price(symbol)

                        place_tp_sl(symbol, SIDE_BUY, entry_price, quantity)

                    elif signal == "SELL":

                        place_market_order(symbol, SIDE_SELL, quantity)
                        time.sleep(2)

                        entry_price = get_entry_price(symbol)

                        place_tp_sl(symbol, SIDE_SELL, entry_price, quantity)

                    time.sleep(1)

                    # =========================
                    # STORE TRADE
                    # =========================
                    trade_times[symbol] = {
                        "entry_time": datetime.now(),
                        "side": signal
                    }

                    # =========================
                    # LOG SUMMARY
                    # =========================
                    orderCounts = get_open_position_counts()

                    log_info(
                        f"*** {symbol} TRADE OPENED ***\n"
                        f"ENTRY TIME: {trade_times[symbol]['entry_time']}\n"
                        f"Wallet Balance: {balance} USDT\n"
                        f"Margin Balance: {totalMarginBalance} USDT\n"
                        f"Unrealized PNL: {totalUnrealizedProfit} USDT\n"
                        f"TOTAL: {orderCounts['total']} | "
                        f"BUY: {orderCounts['buy']} | "
                        f"SELL: {orderCounts['sell']}"
                    )

                    time.sleep(2)

                except Exception as e:
                    log_error(f"{symbol} error: {e}")

            log_info("Waiting for next scan...")
            time.sleep(30)

        except Exception as e:
            log_error(f"MAIN LOOP ERROR: {e}")
            time.sleep(30)


if __name__ == "__main__":
    run_bot()