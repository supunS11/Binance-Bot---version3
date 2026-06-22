import time
from datetime import datetime

import config

from binance.enums import SIDE_BUY, SIDE_SELL

from exchange import (
    get_klines,
    get_balance,
    place_market_order,
    place_tp_sl,
    get_open_position_details,
    get_open_position_counts,
    get_supported_symbols,
    get_futures_participation,
    get_mark_price,
    set_margin_type,
    setup_leverage,
    get_entry_price,
    validate_min_notional,
    cancel_open_protection_orders
)

from indicators import apply_indicators
from strategy import (
    analyze_signal,
    log_signal_analysis,
    should_fetch_futures_context,
    validate_live_entry_guard,
    validate_adverse_zone_level,
    validate_structure_take_profit,
    validate_entry_profit_room,
    validate_dca_structure_level
)
from risk_management import calculate_position_size
from signal_journal import append_signal_journal
from trade_state import (
    create_position_state,
    get_position_state,
    load_trade_state,
    prune_closed_positions,
    record_dca_fill,
    upsert_position_state
)
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


def get_open_position_amounts(position_details):
    return {
        symbol: item["amount"]
        for symbol, item in (position_details or {}).items()
    }


def get_signal_frames(symbol, btc_trend_df):
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
        return None, None, None

    if len(trend_df) < 220 or len(confirm_df) < 220 or len(entry_df) < 220:
        return None, None, None

    if not trend_indicators_ready:
        trend_df = apply_indicators(trend_df)

    confirm_df = apply_indicators(confirm_df)
    entry_df = apply_indicators(entry_df)

    if trend_df is None or confirm_df is None or entry_df is None:
        return None, None, None

    return trend_df, confirm_df, entry_df


def check_live_entry_guard(symbol, side, current_price, mark_price=None):
    if not config.LIVE_ENTRY_CONFIRMATION_ENABLED:
        return True, current_price, {"reason": "LIVE_ENTRY_GUARD_DISABLED"}

    fast_guard_df = get_klines(
        symbol,
        config.LIVE_ENTRY_FAST_TIMEFRAME,
        config.LIVE_ENTRY_KLINE_LIMIT
    )
    slow_guard_df = get_klines(
        symbol,
        config.LIVE_ENTRY_SLOW_TIMEFRAME,
        config.LIVE_ENTRY_KLINE_LIMIT
    )

    if mark_price is None:
        mark_price = get_mark_price(symbol)

    if mark_price is not None:
        current_price = mark_price

    guard_ok, guard_info = validate_live_entry_guard(
        side,
        fast_guard_df,
        slow_guard_df,
        mark_price
    )

    return guard_ok, current_price, guard_info


def log_live_guard_block(symbol, guard_info):
    reason = guard_info.get("reason")
    fast = guard_info.get("fast", {})
    slow = guard_info.get("slow", {})
    log_warning(
        f"{symbol} LIVE ENTRY BLOCKED | {reason} | "
        f"FAST={fast.get('label')} "
        f"SB={fast.get('structure_break')} "
        f"REV={fast.get('opposite_reversal')} | "
        f"SLOW={slow.get('label')} "
        f"SB={slow.get('structure_break')} "
        f"REV={slow.get('opposite_reversal')}"
    )


def log_profit_room_ok(symbol, side, room_info, prefix=""):
    level = room_info.get("raw_level")

    if level is None:
        log_info(f"{symbol} {prefix}PROFIT ROOM OK | {room_info.get('reason')}")
        return

    label = "RESISTANCE" if side == "BUY" else "SUPPORT"
    log_info(
        f"{symbol} {prefix}{label} ROOM OK | "
        f"LEVEL={level} | "
        f"TARGET={room_info.get('target_price')} | "
        f"ROOM_ROI={room_info.get('target_roi')}% | "
        f"SRC={room_info.get('source')}"
    )


def log_dca_structure_level(symbol, side, level_info):
    label = "SUPPORT" if side == "BUY" else "RESISTANCE"
    log_info(
        f"{symbol} DCA {label} LEVEL OK | "
        f"KIND={level_info.get('kind')} | "
        f"LEVEL={level_info.get('level')} | "
        f"ZONE={level_info.get('zone_low')}..{level_info.get('zone_high')} | "
        f"DIST_ROI={level_info.get('distance_roi')}% | "
        f"SCORE={level_info.get('score')} | "
        f"SRC={level_info.get('source')} | "
        f"REACTION={level_info.get('reaction')}"
    )


def get_initial_trade_margin():
    if not config.DCA_ENABLED:
        return config.MARGIN_PER_TRADE

    pct = max(float(config.DCA_INITIAL_MARGIN_PCT), 0)
    return round(config.MARGIN_PER_TRADE * pct / 100, 8)


def get_dca_order_margin(dca_count):
    if not config.DCA_ENABLED:
        return 0

    if dca_count >= config.DCA_MAX_ORDERS:
        return 0

    if dca_count >= len(config.DCA_MARGIN_PCTS):
        return 0

    pct = max(float(config.DCA_MARGIN_PCTS[dca_count]), 0)
    return round(config.MARGIN_PER_TRADE * pct / 100, 8)


def get_dca_trigger_roi(dca_count):
    if dca_count >= config.DCA_MAX_ORDERS:
        return None

    if dca_count >= len(config.DCA_TRIGGER_ROIS):
        return None

    return float(config.DCA_TRIGGER_ROIS[dca_count])


def get_position_adverse_roi(side, avg_entry, current_price):
    if avg_entry <= 0 or current_price <= 0:
        return 0

    if side == "BUY":
        return round(((avg_entry - current_price) / avg_entry) * config.LEVERAGE * 100, 2)

    return round(((current_price - avg_entry) / avg_entry) * config.LEVERAGE * 100, 2)


def get_dca_price_gap_roi(side, anchor_price, current_price):
    if anchor_price <= 0 or current_price <= 0:
        return 0

    if side == "BUY":
        return round(((anchor_price - current_price) / anchor_price) * config.LEVERAGE * 100, 2)

    return round(((current_price - anchor_price) / anchor_price) * config.LEVERAGE * 100, 2)


def seconds_since(timestamp):
    if not timestamp:
        return None

    try:
        return (datetime.now() - datetime.fromisoformat(timestamp)).total_seconds()
    except Exception:
        return None


def adopt_existing_position_state(state, symbol, position_detail):
    if not config.DCA_MANAGE_EXISTING_POSITIONS:
        return None

    entry_price = float(position_detail.get("entry_price", 0) or 0)

    if entry_price <= 0:
        entry_price = float(position_detail.get("mark_price", 0) or 0)

    if entry_price <= 0:
        log_warning(f"{symbol} existing position not adopted | missing entry price")
        return None

    item = create_position_state(
        symbol,
        position_detail["side"],
        entry_price,
        abs(float(position_detail.get("amount", 0))),
        config.MARGIN_PER_TRADE,
        0,
        entry_price,
        {"source": "ADOPTED_EXISTING_POSITION"}
    )
    item["adopted_existing"] = True
    upsert_position_state(state, symbol, item)
    log_warning(f"{symbol} existing position adopted into DCA state")
    return item


def get_updated_position_after_fill(symbol, old_avg, old_quantity, fill_price, fill_quantity):
    details = get_open_position_details(symbol)
    position_detail = (details or {}).get(symbol)

    if position_detail:
        return (
            float(position_detail.get("entry_price", 0) or fill_price),
            abs(float(position_detail.get("amount", 0))),
            position_detail
        )

    total_quantity = old_quantity + fill_quantity

    if total_quantity <= 0:
        return fill_price, fill_quantity, None

    avg_entry = (
        (old_avg * old_quantity) +
        (fill_price * fill_quantity)
    ) / total_quantity

    return avg_entry, total_quantity, None


def manage_dca_position(symbol, state, position_detail, btc_trend_df, btc_trend):
    if not config.DCA_ENABLED:
        log_warning(f"{symbol} already has open position")
        return

    position_state = get_position_state(state, symbol)

    if not position_state:
        position_state = adopt_existing_position_state(
            state,
            symbol,
            position_detail
        )

    if not position_state or not position_state.get("managed_by_bot"):
        log_warning(f"{symbol} open position is not bot-managed; DCA skipped")
        return

    side = position_state.get("side") or position_detail.get("side")

    if side not in ("BUY", "SELL"):
        log_warning(f"{symbol} DCA skipped | invalid side in state")
        return

    live_side = position_detail.get("side")

    if live_side and live_side != side:
        log_warning(
            f"{symbol} DCA skipped | state side {side} != live side {live_side}"
        )
        return

    dca_count = int(position_state.get("dca_count", 0) or 0)
    dca_margin = get_dca_order_margin(dca_count)
    trigger_roi = get_dca_trigger_roi(dca_count)

    if dca_margin <= 0 or trigger_roi is None:
        log_info(f"{symbol} DCA complete or not configured")
        return

    last_order_at = (
        position_state.get("last_dca_at") or
        position_state.get("opened_at")
    )
    elapsed = seconds_since(last_order_at)

    if (
        elapsed is not None
        and elapsed < config.DCA_MIN_SECONDS_BETWEEN_ORDERS
    ):
        remaining = int(config.DCA_MIN_SECONDS_BETWEEN_ORDERS - elapsed)
        log_info(f"{symbol} DCA waiting cooldown | {remaining}s remaining")
        return

    avg_entry = float(
        position_detail.get("entry_price") or
        position_state.get("avg_entry") or
        0
    )
    old_quantity = abs(float(position_detail.get("amount", 0)))
    mark_price = get_mark_price(symbol)

    if avg_entry <= 0 or old_quantity <= 0 or mark_price is None:
        log_warning(f"{symbol} DCA skipped | position price unavailable")
        return

    adverse_roi = get_position_adverse_roi(side, avg_entry, mark_price)

    if adverse_roi < trigger_roi:
        log_info(
            f"{symbol} DCA not triggered | "
            f"ADVERSE_ROI={adverse_roi}% < TRIGGER={trigger_roi}%"
        )
        return

    if adverse_roi > config.DCA_MAX_ADVERSE_ROI:
        log_warning(
            f"{symbol} DCA skipped | "
            f"ADVERSE_ROI={adverse_roi}% > MAX={config.DCA_MAX_ADVERSE_ROI}%"
        )
        return

    anchor_price = float(
        position_state.get("last_dca_price") or
        position_state.get("initial_entry") or
        avg_entry
    )
    gap_roi = get_dca_price_gap_roi(side, anchor_price, mark_price)

    if gap_roi < config.DCA_MIN_PRICE_GAP_ROI:
        log_info(
            f"{symbol} DCA waiting wider price gap | "
            f"GAP={gap_roi}% < MIN={config.DCA_MIN_PRICE_GAP_ROI}%"
        )
        return

    trend_df, confirm_df, entry_df = get_signal_frames(symbol, btc_trend_df)

    if trend_df is None or confirm_df is None or entry_df is None:
        log_warning(f"{symbol} DCA skipped | signal data unavailable")
        return

    btc_corr, rs = calculate_btc_context(symbol, trend_df, btc_trend_df)
    analysis = analyze_signal(
        trend_df,
        confirm_df,
        entry_df,
        btc_trend,
        btc_corr,
        rs,
        log_details=False
    )
    side_analysis = analysis.get(side.lower(), {})
    opposite_key = "sell" if side == "BUY" else "buy"
    opposite = analysis.get(opposite_key, {})

    if config.DCA_REQUIRE_TREND_CONFIRMATION and not (
        side_analysis.get("trend_ok") and side_analysis.get("confirm_ok")
    ):
        log_warning(
            f"{symbol} DCA skipped | higher timeframe no longer confirms {side}"
        )
        append_signal_journal(
            symbol,
            analysis,
            None,
            trend_df,
            confirm_df,
            entry_df,
            btc_trend,
            btc_corr,
            rs,
            action="DCA_SKIPPED",
            skip_reason="DCA_TREND_CONFIRMATION_FAILED"
        )
        return

    if (
        opposite.get("hard_ok")
        and opposite.get("confidence", 0) >= (
            side_analysis.get("confidence", 0) + config.LONG_TERM_MIN_SIGNAL_EDGE
        )
    ):
        log_warning(f"{symbol} DCA skipped | opposite signal is stronger")
        append_signal_journal(
            symbol,
            analysis,
            None,
            trend_df,
            confirm_df,
            entry_df,
            btc_trend,
            btc_corr,
            rs,
            action="DCA_SKIPPED",
            skip_reason="DCA_OPPOSITE_SIGNAL_STRONGER"
        )
        return

    guard_ok, current_price, guard_info = check_live_entry_guard(
        symbol,
        side,
        mark_price,
        mark_price=mark_price
    )

    if not guard_ok:
        log_live_guard_block(symbol, guard_info)
        append_signal_journal(
            symbol,
            analysis,
            None,
            trend_df,
            confirm_df,
            entry_df,
            btc_trend,
            btc_corr,
            rs,
            action="DCA_SKIPPED",
            skip_reason=guard_info.get("reason")
        )
        return

    room_ok, room_info = validate_entry_profit_room(
        side,
        current_price,
        trend_df,
        confirm_df,
        leverage=config.LEVERAGE
    )

    if not room_ok:
        log_warning(f"{symbol} DCA skipped | {room_info.get('reason')}")
        append_signal_journal(
            symbol,
            analysis,
            None,
            trend_df,
            confirm_df,
            entry_df,
            btc_trend,
            btc_corr,
            rs,
            action="DCA_SKIPPED",
            skip_reason=room_info.get("reason")
        )
        return

    log_profit_room_ok(symbol, side, room_info, prefix="DCA ")

    level_ok, level_info = validate_dca_structure_level(
        side,
        current_price,
        trend_df,
        confirm_df,
        entry_df,
        leverage=config.LEVERAGE
    )

    if not level_ok:
        log_warning(f"{symbol} DCA skipped | {level_info.get('reason')}")
        append_signal_journal(
            symbol,
            analysis,
            None,
            trend_df,
            confirm_df,
            entry_df,
            btc_trend,
            btc_corr,
            rs,
            action="DCA_SKIPPED",
            skip_reason=level_info.get("reason")
        )
        return

    log_dca_structure_level(symbol, side, level_info)

    balance = get_balance()
    quantity = calculate_position_size(
        balance,
        current_price,
        level_info["level"],
        symbol,
        dca_margin
    )

    if quantity <= 0:
        log_warning(f"{symbol} DCA skipped | invalid quantity")
        return

    notional_ok, notional = validate_min_notional(
        symbol,
        quantity,
        current_price
    )

    if not notional_ok:
        log_warning(f"{symbol} DCA skipped | notional too low: {notional}")
        return

    if not set_margin_type(symbol):
        return

    if not setup_leverage(symbol):
        return

    order_side = SIDE_BUY if side == "BUY" else SIDE_SELL
    order = place_market_order(symbol, order_side, quantity)

    if not order:
        return

    fill_price = get_entry_price(symbol, order)

    if fill_price <= 0:
        fill_price = current_price
        log_warning(f"{symbol} DCA fill price unavailable | using current price")

    avg_entry, total_quantity, updated_position = get_updated_position_after_fill(
        symbol,
        avg_entry,
        old_quantity,
        fill_price,
        quantity
    )

    record_dca_fill(
        state,
        symbol,
        avg_entry,
        total_quantity,
        dca_margin,
        fill_price,
        level_info
    )

    structure_tp = None

    if not config.STATIC_TP_ENABLED:
        tp_ok, structure_tp = validate_structure_take_profit(
            side,
            avg_entry,
            trend_df,
            confirm_df,
            leverage=config.LEVERAGE
        )

        if tp_ok:
            log_info(
                f"{symbol} DCA STRUCTURE TP | "
                f"TARGET={structure_tp['target_price']} | "
                f"ROI={structure_tp['target_roi']}% | "
                f"SRC={structure_tp['source']}"
            )
        else:
            log_warning(
                f"{symbol} DCA {structure_tp['reason']} | "
                f"USING FALLBACK ROI TP"
            )

    if config.DCA_REPRICE_TP_AFTER_FILL:
        if cancel_open_protection_orders(symbol):
            protection_ok = place_tp_sl(
                symbol,
                order_side,
                avg_entry,
                total_quantity,
                confirm_df,
                structure_tp=structure_tp
            )

            if not protection_ok:
                log_warning(f"{symbol} DCA TP ORDER NOT CREATED")

    append_signal_journal(
        symbol,
        analysis,
        None,
        trend_df,
        confirm_df,
        entry_df,
        btc_trend,
        btc_corr,
        rs,
        action="DCA_FILLED"
    )

    log_info(
        f"*** {symbol} DCA FILLED ***\n"
        f"SIDE: {side}\n"
        f"FILL: {fill_price}\n"
        f"AVG_ENTRY: {avg_entry}\n"
        f"QTY_TOTAL: {total_quantity}\n"
        f"DCA_COUNT: {dca_count + 1}/{config.DCA_MAX_ORDERS}\n"
        f"ADVERSE_ROI_AT_TRIGGER: {adverse_roi}%\n"
    )

    if updated_position:
        position_detail.update(updated_position)


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
            position_details = get_open_position_details()

            if position_details is None:
                log_warning("Position snapshot unavailable; skipping this scan")
                time.sleep(config.SCAN_SLEEP_SECONDS)
                continue

            open_positions = get_open_position_amounts(position_details)
            trade_state = load_trade_state()
            prune_closed_positions(trade_state, open_positions)
            log_closed_trades(open_positions)

            btc_trend_df, btc_trend = get_cached_btc_context()
            log_info(f"BTC TREND: {btc_trend}")
            futures_context_fetches = 0

            for symbol in scan_symbols:

                try:

                    log_info(f"Checking {symbol}")

                    # =========================
                    # POSITION CHECK
                    # =========================
                    if symbol in open_positions:
                        manage_dca_position(
                            symbol,
                            trade_state,
                            position_details[symbol],
                            btc_trend_df,
                            btc_trend
                        )
                        continue

                    # =========================
                    # DATA
                    # =========================
                    trend_df, confirm_df, entry_df = get_signal_frames(
                        symbol,
                        btc_trend_df
                    )

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
                    base_analysis = analyze_signal(
                        trend_df,
                        confirm_df,
                        entry_df,
                        btc_trend,
                        btc_corr,
                        rs,
                        log_details=False
                    )
                    participation = None
                    final_analysis = base_analysis

                    if should_fetch_futures_context(base_analysis):
                        if (
                            futures_context_fetches <
                            config.FUTURES_CONTEXT_MAX_SYMBOLS_PER_SCAN
                        ):
                            participation = get_futures_participation(symbol)
                            futures_context_fetches += 1

                            log_info(
                                f"{symbol} FUTURES CONTEXT | "
                                f"OI={participation.get('oi_change_pct')}% | "
                                f"TAKER={participation.get('taker_buy_sell_ratio')} | "
                                f"GLOBAL_LS={participation.get('global_long_short_ratio')} | "
                                f"TOP_LS={participation.get('top_long_short_ratio')} | "
                                f"FUNDING={participation.get('funding_rate')}"
                            )

                            final_analysis = analyze_signal(
                                trend_df,
                                confirm_df,
                                entry_df,
                                btc_trend,
                                btc_corr,
                                rs,
                                participation=participation,
                                log_details=True
                            )
                        else:
                            log_warning(
                                f"{symbol} FUTURES CONTEXT SKIPPED | "
                                f"SCAN LIMIT={config.FUTURES_CONTEXT_MAX_SYMBOLS_PER_SCAN}"
                            )
                            log_signal_analysis(final_analysis)
                    else:
                        log_signal_analysis(final_analysis)

                    signal = final_analysis["signal"]

                    if not signal:
                        append_signal_journal(
                            symbol,
                            final_analysis,
                            participation,
                            trend_df,
                            confirm_df,
                            entry_df,
                            btc_trend,
                            btc_corr,
                            rs,
                            action="NO_SIGNAL",
                            skip_reason="NO_FINAL_SIGNAL"
                        )
                        log_warning(
                            f"{symbol} NO SIGNAL | "
                            f"BTC={btc_trend} | "
                            f"CORR={btc_corr} | "
                            f"RS={rs}"
                        )
                        continue

                    append_signal_journal(
                        symbol,
                        final_analysis,
                        participation,
                        trend_df,
                        confirm_df,
                        entry_df,
                        btc_trend,
                        btc_corr,
                        rs,
                        action="SIGNAL"
                    )

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
                    # LIVE ENTRY REVERSAL GUARD
                    # =========================
                    if config.LIVE_ENTRY_CONFIRMATION_ENABLED:
                        guard_ok, current_price, guard_info = check_live_entry_guard(
                            symbol,
                            signal,
                            current_price
                        )

                        if not guard_ok:
                            log_live_guard_block(symbol, guard_info)
                            append_signal_journal(
                                symbol,
                                final_analysis,
                                participation,
                                trend_df,
                                confirm_df,
                                entry_df,
                                btc_trend,
                                btc_corr,
                                rs,
                                action="SKIPPED_LIVE_GUARD",
                                skip_reason=guard_info.get("reason")
                            )
                            continue

                        log_info(
                            f"{symbol} LIVE ENTRY GUARD OK | "
                            f"MARK={current_price} | {guard_info.get('reason')}"
                        )

                    # =========================
                    # PROFIT-SIDE ROOM CHECK
                    # =========================
                    room_ok, room_info = validate_entry_profit_room(
                        signal,
                        current_price,
                        trend_df,
                        confirm_df,
                        leverage=config.LEVERAGE
                    )

                    if not room_ok:
                        log_warning(
                            f"{symbol} SKIP | {room_info.get('reason')}"
                        )
                        append_signal_journal(
                            symbol,
                            final_analysis,
                            participation,
                            trend_df,
                            confirm_df,
                            entry_df,
                            btc_trend,
                            btc_corr,
                            rs,
                            action="SKIPPED_PROFIT_ROOM",
                            skip_reason=room_info.get("reason")
                        )
                        continue

                    log_profit_room_ok(symbol, signal, room_info)

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
                    # POSITION SIZE
                    # =========================
                    balance = get_balance()
                    initial_margin = get_initial_trade_margin()

                    quantity = calculate_position_size(
                        balance,
                        current_price,
                        reference_price,
                        symbol,
                        initial_margin
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

                    structure_tp = None

                    if not config.STATIC_TP_ENABLED:
                        tp_ok, structure_tp = validate_structure_take_profit(
                            signal,
                            entry_price,
                            trend_df,
                            confirm_df,
                            leverage=config.LEVERAGE
                        )

                        if tp_ok:
                            log_info(
                                f"{symbol} STRUCTURE TP | "
                                f"TARGET={structure_tp['target_price']} | "
                                f"RAW_LEVEL={structure_tp['raw_level']} | "
                                f"ROI={structure_tp['target_roi']}% | "
                                f"SRC={structure_tp['source']}"
                            )
                        else:
                            log_warning(
                                f"{symbol} {structure_tp['reason']} | "
                                f"USING FALLBACK ROI TP"
                            )

                    # =========================
                    # PLACE TP/SL
                    # =========================
                    protection_ok = place_tp_sl(
                        symbol,
                        side,
                        entry_price,
                        quantity,
                        confirm_df,
                        structure_tp=structure_tp
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
                    upsert_position_state(
                        trade_state,
                        symbol,
                        create_position_state(
                            symbol,
                            signal,
                            entry_price,
                            quantity,
                            config.MARGIN_PER_TRADE,
                            initial_margin,
                            reference_price,
                            level_info
                        )
                    )
                    append_signal_journal(
                        symbol,
                        final_analysis,
                        participation,
                        trend_df,
                        confirm_df,
                        entry_df,
                        btc_trend,
                        btc_corr,
                        rs,
                        action="TRADE_OPENED"
                    )

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
