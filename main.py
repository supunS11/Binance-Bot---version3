import time
from datetime import datetime

import config

from binance.enums import SIDE_BUY, SIDE_SELL

from exchange import (
    get_klines,
    get_balance,
    place_market_order,
    place_tp_sl,
    get_open_positions,
    get_open_position_counts,
    get_supported_symbols,
    set_margin_type,
    setup_leverage,
    get_entry_price,
    validate_min_notional
)

from indicators import apply_indicators
from strategy import check_signal, validate_adverse_zone_level
from risk_management import calculate_position_size
from logger import log_info, log_warning, log_error


trade_times = {}


def get_scan_symbols():
    symbols = list(dict.fromkeys(config.SYMBOLS))

    if config.MAX_SCAN_SYMBOLS > 0:
        symbols = symbols[:config.MAX_SCAN_SYMBOLS]

    supported_symbols = get_supported_symbols()

    if not supported_symbols:
        return symbols

    scan_symbols = [
        symbol
        for symbol in symbols
        if symbol in supported_symbols
    ]

    skipped = len(symbols) - len(scan_symbols)

    if skipped > 0:
        log_warning(f"Skipped {skipped} unsupported symbols from scan list")

    return scan_symbols


def log_closed_trades(open_positions):
    for symbol in list(trade_times):
        if symbol in open_positions:
            continue

        exit_time = datetime.now()
        entry_time = trade_times[symbol]["entry_time"]
        duration = exit_time - entry_time

        log_info(
            f"*** {symbol} TRADE CLOSED *** | "
            f"ENTRY: {entry_time} | "
            f"EXIT: {exit_time} | "
            f"DURATION: {duration}"
        )

        del trade_times[symbol]


def get_cached_btc_context():
    btc_df = get_klines("BTCUSDT", config.TREND_TIMEFRAME)

    if btc_df is None or len(btc_df) < 220:
        return None, "NEUTRAL"

    btc_df = apply_indicators(btc_df)

    if btc_df is None:
        return None, "NEUTRAL"

    btc = btc_df.iloc[-2]

    if btc["ema50"] > btc["ema200"]:
        return btc_df, "BULLISH"

    if btc["ema50"] < btc["ema200"]:
        return btc_df, "BEARISH"

    return btc_df, "NEUTRAL"


def calculate_btc_context(symbol, trend_df, btc_df):
    if symbol == "BTCUSDT":
        return 1.0, 0

    if trend_df is None or btc_df is None:
        return 0, 0

    try:
        coin_close = trend_df["close"].tail(100).reset_index(drop=True)
        btc_close = btc_df["close"].tail(100).reset_index(drop=True)
        length = min(len(coin_close), len(btc_close))

        if length < 20:
            btc_corr = 0
        else:
            coin_ret = coin_close.tail(length).pct_change().dropna()
            btc_ret = btc_close.tail(length).pct_change().dropna()
            btc_corr = coin_ret.corr(btc_ret)

            if btc_corr != btc_corr:
                btc_corr = 0

        if length < 10:
            rs = 0
        else:
            coin_tail = coin_close.tail(length)
            btc_tail = btc_close.tail(length)
            coin_r = (
                (coin_tail.iloc[-1] - coin_tail.iloc[-10]) /
                coin_tail.iloc[-10]
            ) * 100
            btc_r = (
                (btc_tail.iloc[-1] - btc_tail.iloc[-10]) /
                btc_tail.iloc[-10]
            ) * 100
            rs = coin_r - btc_r

        return round(float(btc_corr), 2), round(float(rs), 2)

    except Exception as e:
        log_error(f"{symbol} BTC context error: {e}")
        return 0, 0


def run_bot():

    log_info("BOT STARTED")
    scan_symbols = get_scan_symbols()
    log_info(
        f"Scanning {len(scan_symbols)} symbols | "
        f"KLINE_LIMIT={config.KLINE_LIMIT} | "
        f"THROTTLE={config.REQUEST_THROTTLE_SECONDS}s"
    )

    while True:

        try:
            open_positions = get_open_positions()

            if open_positions is None:
                log_warning("Position snapshot unavailable; skipping this scan")
                time.sleep(config.SCAN_SLEEP_SECONDS)
                continue

            log_closed_trades(open_positions)

            btc_trend_df, btc_trend = get_cached_btc_context()
            log_info(f"BTC TREND: {btc_trend}")

            for symbol in scan_symbols:

                try:

                    log_info(f"Checking {symbol}")

                    # =========================
                    # POSITION CHECK
                    # =========================
                    if symbol in open_positions:
                        log_warning(f"{symbol} already has open position")
                        continue

                    # =========================
                    # DATA
                    # =========================
                    trend_indicators_ready = (
                        symbol == "BTCUSDT" and btc_trend_df is not None
                    )

                    if trend_indicators_ready:
                        trend_df = btc_trend_df
                    else:
                        trend_df = get_klines(symbol, config.TREND_TIMEFRAME)

                    confirm_df = get_klines(symbol, config.CONFIRMATION_TIMEFRAME)
                    entry_df = get_klines(symbol, config.ENTRY_TIMEFRAME)

                    if trend_df is None or confirm_df is None or entry_df is None:
                        continue

                    if len(trend_df) < 220 or len(confirm_df) < 220 or len(entry_df) < 220:
                        continue

                    # =========================
                    # INDICATORS
                    # =========================
                    if not trend_indicators_ready:
                        trend_df = apply_indicators(trend_df)

                    confirm_df = apply_indicators(confirm_df)
                    entry_df = apply_indicators(entry_df)

                    if trend_df is None or confirm_df is None or entry_df is None:
                        continue

                    # =========================
                    # BTC CONTEXT
                    # =========================
                    btc_corr, rs = calculate_btc_context(
                        symbol,
                        trend_df,
                        btc_trend_df
                    )

                    log_info(f"{symbol} BTC CORR: {btc_corr}")
                    log_info(f"{symbol} RS: {rs}%")

                    # =========================
                    # SIGNAL
                    # =========================
                    signal = check_signal(
                        trend_df,
                        confirm_df,
                        entry_df,
                        btc_trend,
                        btc_corr,
                        rs
                    )

                    if not signal:
                        log_warning(
                            f"{symbol} NO SIGNAL | "
                            f"BTC={btc_trend} | "
                            f"CORR={btc_corr} | "
                            f"RS={rs}"
                        )
                        continue

                    log_info(f"{symbol} SIGNAL: {signal}")

                    # =========================
                    # POSITION LIMITS
                    # =========================
                    counts = get_open_position_counts(open_positions)

                    if config.MAX_TOTAL_POSITIONS and counts['total'] >= config.MAX_TOTAL_POSITIONS:
                        log_warning(
                            f"🚨 MAX POSITIONS REACHED 🚨\n"
                            f"TOTAL OPEN: {counts['total']}/{config.MAX_TOTAL_POSITIONS}\n"
                            f"BUY: {counts['buy']} | SELL: {counts['sell']}\n"
                            f"Skipping new entries..."
    )
                        continue

                    if signal == "BUY" and config.MAX_BUY_POSITIONS and counts['buy'] >= config.MAX_BUY_POSITIONS:
                        log_warning(
                            f"🚨 MAX BUY POSITIONS REACHED | "
                            f"BUY={counts['buy']}/{config.MAX_BUY_POSITIONS} | "
                            f"TOTAL={counts['total']}"
                        )
                        continue

                    if signal == "SELL" and config.MAX_SELL_POSITIONS and counts['sell'] >= config.MAX_SELL_POSITIONS:
                        log_warning(
                            f"🚨 MAX SELL POSITIONS REACHED | "
                            f"SELL={counts['sell']}/{config.MAX_SELL_POSITIONS} | "
                            f"TOTAL={counts['total']}"
                        )
                        continue

                    # =========================
                    # PRICE (PRE-ENTRY)
                    # =========================
                    current_price = entry_df['close'].iloc[-2]

                    # =========================
                    # LONG-TERM ADVERSE-ZONE SUPPORT / RESISTANCE
                    # =========================
                    level_ok, level_info = validate_adverse_zone_level(
                        signal,
                        current_price,
                        trend_df,
                        confirm_df,
                        leverage=config.LEVERAGE
                    )

                    if not level_ok:
                        log_warning(
                            f"{symbol} SKIP | {level_info.get('reason')}"
                        )
                        continue

                    reference_price = level_info["level"]
                    adverse_roi = level_info["adverse_roi"]
                    level_label = "SUPPORT" if signal == "BUY" else "RESISTANCE"

                    log_info(
                        f"{symbol} {level_label} SAFETY LEVEL | "
                        f"PRICE={reference_price} | ROI={adverse_roi}% | "
                        f"SCORE={level_info['score']} | SRC={level_info['source']}"
                    )

                    # =========================
                    # POSITION SIZE (FIXED)
                    # =========================
                    balance = get_balance()

                    quantity = calculate_position_size(
                        balance,
                        current_price,
                        reference_price,
                        symbol,
                        config.MARGIN_PER_TRADE
                    )

                    notional = quantity * current_price

                    log_info(
                        f"{symbol} QTY={quantity} | NOTIONAL={notional:.2f}"
                    )

                    if quantity <= 0:
                        log_warning(f"{symbol} SKIPPED | INVALID QTY")
                        continue

                    log_info(f"{symbol} QTY: {quantity}")

                    notional_ok, notional = validate_min_notional(
                        symbol,
                        quantity,
                        current_price
                    )

                    if not notional_ok:
                        log_warning(f"{symbol} SKIP | NOTIONAL TOO LOW: {notional}")
                        continue

                    # =========================
                    # MARGIN / LEVERAGE
                    # =========================
                    if not set_margin_type(symbol):
                        continue

                    if not setup_leverage(symbol):
                        continue

                    # =========================
                    # PLACE ORDER
                    # =========================
                    side = SIDE_BUY if signal == "BUY" else SIDE_SELL

                    order = place_market_order(symbol, side, quantity)

                    if not order:
                        continue

                    entry_price = get_entry_price(symbol, order)

                    if entry_price <= 0:
                        entry_price = current_price
                        log_warning(
                            f"{symbol} ENTRY PRICE UNAVAILABLE | "
                            f"USING CURRENT PRICE FOR TP"
                        )

                    # =========================
                    # PLACE TP/SL
                    # =========================
                    protection_ok = place_tp_sl(
                        symbol,
                        side,
                        entry_price,
                        quantity,
                        confirm_df
                    )

                    if not protection_ok:
                        log_warning(f"{symbol} TP ORDER NOT CREATED")

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
                    log_info(
                        f"*** {symbol} TRADE OPENED ***\n"
                        f"ENTRY: {entry_price}\n"
                        f"{level_label}: {reference_price}\n"
                        f"ADVERSE ROI TO LEVEL: {adverse_roi}%\n"
                        f"SL: {'ENABLED' if config.SL_ENABLED else 'DISABLED'}\n"
                        f"BALANCE: {balance}\n"
                    )

                    open_positions[symbol] = quantity if signal == "BUY" else -quantity
                    orderCounts = get_open_position_counts(open_positions)

                    log_info(
                        f"{symbol} OPENED | TOTAL={orderCounts['total']} | "
                        f"BUY={orderCounts['buy']} | SELL={orderCounts['sell']}"
                    )

                    if config.POST_TRADE_SLEEP_SECONDS > 0:
                        time.sleep(config.POST_TRADE_SLEEP_SECONDS)

                except Exception as e:
                    log_error(f"{symbol} ERROR: {e}")

            log_info("Waiting next scan...")
            time.sleep(config.SCAN_SLEEP_SECONDS)

        except Exception as e:
            log_error(f"MAIN LOOP ERROR: {e}")
            time.sleep(config.SCAN_SLEEP_SECONDS)


if __name__ == "__main__":
    run_bot()
