import config
from logger import log_info, log_error, log_warning


def score_to_confidence(score, max_score=34):
    if score <= 0:
        return 0

    return round(min((score / max_score) * 100, 100), 2)


def get_config_float(name, default):
    try:
        return float(getattr(config, name, default))
    except (TypeError, ValueError):
        return default


def get_config_int(name, default):
    try:
        return int(getattr(config, name, default))
    except (TypeError, ValueError):
        return default


def latest_closed(df):
    return df.iloc[-2] if len(df) > 1 else df.iloc[-1]


def previous_closed(df):
    return df.iloc[-3] if len(df) > 2 else df.iloc[-2]


def pct_distance(a, b):
    if not b:
        return 0

    return abs(a - b) / b * 100


def add_score(score, condition, points):
    return score + points if condition else score


def get_structure_stop_loss(df, side):
    try:
        candle = latest_closed(df)
        atr = candle["atr"]

        if atr <= 0:
            return None

        if side == "BUY":
            swing_low = df["low"].iloc[-20:-1].min()
            return swing_low - (atr * 1.5)

        swing_high = df["high"].iloc[-20:-1].max()
        return swing_high + (atr * 1.5)

    except Exception as e:
        log_error(f"STRUCTURE REFERENCE ERROR: {e}")
        return None


def detect_market_structure(df):
    try:
        recent_high = df["high"].iloc[-30:-5].max()
        recent_low = df["low"].iloc[-30:-5].min()
        prev_high = df["high"].iloc[-60:-30].max()
        prev_low = df["low"].iloc[-60:-30].min()
        close = latest_closed(df)["close"]

        return {
            "bullish_structure": recent_high > prev_high and recent_low > prev_low,
            "bearish_structure": recent_high < prev_high and recent_low < prev_low,
            "bullish_breakout": close > recent_high,
            "bearish_breakdown": close < recent_low,
        }

    except Exception:
        return {
            "bullish_structure": False,
            "bearish_structure": False,
            "bullish_breakout": False,
            "bearish_breakdown": False,
        }


def _level_tolerance(df):
    latest = latest_closed(df)
    price = latest["close"]
    atr = latest["atr"] if "atr" in latest.index else 0
    pct_tolerance = price * (
        get_config_float("LONG_TERM_SR_TOLERANCE_PCT", 1.0) / 100
    )
    atr_tolerance = atr * get_config_float("LONG_TERM_SR_ATR_TOLERANCE", 0.75)

    return max(pct_tolerance, atr_tolerance, price * 0.002)


def _collect_pivot_levels(df, side, label, timeframe_weight):
    lookback = get_config_int("LONG_TERM_SR_LOOKBACK", 160)
    min_touches = get_config_int("LONG_TERM_SR_MIN_TOUCHES", 2)
    data = df.tail(lookback).copy()

    if len(data) < 20:
        return []

    tolerance = _level_tolerance(data)
    swing = 3
    column = "low" if side == "BUY" else "high"
    levels = []

    for pos in range(swing, len(data) - swing):
        candle = data.iloc[pos]
        window = data.iloc[pos - swing:pos + swing + 1]
        level = candle[column]

        if side == "BUY" and level > window["low"].min():
            continue

        if side == "SELL" and level < window["high"].max():
            continue

        touches = int((abs(data[column] - level) <= tolerance).sum())

        if touches < min_touches:
            continue

        recency_score = pos / len(data)
        volume_score = 0

        if (
            "volume_sma" in data.columns
            and candle["volume"] > candle["volume_sma"]
        ):
            volume_score = 0.5

        levels.append({
            "level": float(level),
            "score": touches * timeframe_weight + recency_score + volume_score,
            "touches": touches,
            "source": f"{label}_pivot",
        })

    return levels


def _collect_ema_levels(df, side, label, timeframe_weight):
    latest = latest_closed(df)
    levels = []

    for ema_name, bonus in (("ema50", 0.75), ("ema200", 1.25)):
        if ema_name not in latest.index:
            continue

        level = float(latest[ema_name])
        close = float(latest["close"])

        if side == "BUY" and level >= close:
            continue

        if side == "SELL" and level <= close:
            continue

        levels.append({
            "level": level,
            "score": timeframe_weight + bonus,
            "touches": 0,
            "source": f"{label}_{ema_name}",
        })

    return levels


def _collect_range_levels(df, side, label, timeframe_weight):
    lookback = get_config_int("LONG_TERM_SR_LOOKBACK", 160)
    data = df.iloc[:-1].tail(lookback).copy() if len(df) > 1 else df.tail(lookback)

    if len(data) < 20:
        return []

    tolerance = _level_tolerance(data)
    column = "low" if side == "BUY" else "high"
    levels = []

    for window in (20, 50, 100):
        if len(data) < window:
            continue

        recent = data.tail(window)
        level = recent[column].min() if side == "BUY" else recent[column].max()
        touches = int((abs(data[column] - level) <= tolerance).sum())
        score = timeframe_weight + min(window / 100, 1) + (touches * 0.25)

        levels.append({
            "level": float(level),
            "score": round(score, 2),
            "touches": touches,
            "source": f"{label}_{window}_range",
        })

    return levels


def _dedupe_levels(levels, tolerance):
    deduped = []

    for level in sorted(levels, key=lambda item: item["score"], reverse=True):
        if any(abs(level["level"] - item["level"]) <= tolerance for item in deduped):
            continue

        deduped.append(level)

    return deduped


def _take_profit_buffer(entry_price, trend_df, confirm_df):
    pct_buffer = entry_price * (
        get_config_float("STRUCTURE_TP_BUFFER_PCT", 0.15) / 100
    )
    atr_values = []

    for df in (confirm_df, trend_df):
        try:
            atr = latest_closed(df)["atr"]

            if atr > 0:
                atr_values.append(float(atr))
        except Exception:
            continue

    atr_buffer = 0

    if atr_values:
        atr_buffer = min(atr_values) * get_config_float(
            "STRUCTURE_TP_ATR_BUFFER_MULT",
            0.25
        )

    return max(pct_buffer, atr_buffer, entry_price * 0.0005)


def find_structure_take_profit(side, entry_price, trend_df, confirm_df, leverage=None):
    leverage_to_use = leverage or config.LEVERAGE
    target_side = "SELL" if side == "BUY" else "BUY"
    min_roi = get_config_float("STRUCTURE_TP_MIN_ROI", 8)
    max_roi = get_config_float("STRUCTURE_TP_MAX_ROI", 120)
    min_score = get_config_float("STRUCTURE_TP_MIN_SCORE", 2.0)
    buffer = _take_profit_buffer(entry_price, trend_df, confirm_df)

    candidates = []
    candidates.extend(_collect_pivot_levels(trend_df, target_side, "1d", 2.0))
    candidates.extend(_collect_pivot_levels(confirm_df, target_side, "4h", 1.25))
    candidates.extend(_collect_range_levels(trend_df, target_side, "1d", 2.0))
    candidates.extend(_collect_range_levels(confirm_df, target_side, "4h", 1.25))
    candidates.extend(_collect_ema_levels(trend_df, target_side, "1d", 2.0))
    candidates.extend(_collect_ema_levels(confirm_df, target_side, "4h", 1.25))

    tolerance = max(_level_tolerance(trend_df), _level_tolerance(confirm_df))
    candidates = _dedupe_levels(candidates, tolerance)
    valid = []

    for candidate in candidates:
        level = candidate["level"]

        if candidate["score"] < min_score:
            continue

        if side == "BUY":
            if level <= entry_price:
                continue

            target_price = level - buffer

            if target_price <= entry_price:
                continue

            roi = ((target_price - entry_price) / entry_price) * leverage_to_use * 100
        else:
            if level >= entry_price:
                continue

            target_price = level + buffer

            if target_price >= entry_price:
                continue

            roi = ((entry_price - target_price) / entry_price) * leverage_to_use * 100

        if roi < min_roi or roi > max_roi:
            continue

        item = candidate.copy()
        item["target_price"] = float(target_price)
        item["raw_level"] = float(level)
        item["target_roi"] = round(float(roi), 2)
        item["buffer"] = round(float(buffer), 8)
        valid.append(item)

    if not valid:
        return None

    valid.sort(key=lambda item: (item["target_roi"], -item["score"]))
    return valid[0]


def validate_structure_take_profit(side, entry_price, trend_df, confirm_df, leverage=None):
    target = find_structure_take_profit(
        side,
        entry_price,
        trend_df,
        confirm_df,
        leverage=leverage
    )

    if target:
        return True, target

    return False, {
        "reason": "NO VALID STRUCTURE TP LEVEL FOUND"
    }


def find_adverse_zone_level(side, entry_price, trend_df, confirm_df, leverage=None):
    leverage_to_use = leverage or config.LEVERAGE
    max_adverse_roi = abs(get_config_float("LONG_TERM_MAX_ADVERSE_ROI", 50))
    max_price_move = (max_adverse_roi / max(leverage_to_use, 1)) / 100

    if side == "BUY":
        zone_min = entry_price * (1 - max_price_move)
        zone_max = entry_price
    else:
        zone_min = entry_price
        zone_max = entry_price * (1 + max_price_move)

    candidates = []
    candidates.extend(_collect_pivot_levels(trend_df, side, "1d", 2.0))
    candidates.extend(_collect_pivot_levels(confirm_df, side, "4h", 1.25))
    candidates.extend(_collect_ema_levels(trend_df, side, "1d", 2.0))
    candidates.extend(_collect_ema_levels(confirm_df, side, "4h", 1.25))

    tolerance = max(_level_tolerance(trend_df), _level_tolerance(confirm_df))
    candidates = _dedupe_levels(candidates, tolerance)
    valid = []

    for candidate in candidates:
        level = candidate["level"]

        if not (zone_min <= level <= zone_max):
            continue

        if side == "BUY" and level >= entry_price:
            continue

        if side == "SELL" and level <= entry_price:
            continue

        if side == "BUY":
            adverse_roi = ((level - entry_price) / entry_price) * leverage_to_use * 100
        else:
            adverse_roi = ((entry_price - level) / entry_price) * leverage_to_use * 100

        proximity_score = 1 - min(abs(adverse_roi) / max_adverse_roi, 1)
        item = candidate.copy()
        item["adverse_roi"] = round(adverse_roi, 2)
        item["score"] = round(candidate["score"] + proximity_score, 2)
        item["zone_min"] = zone_min
        item["zone_max"] = zone_max
        valid.append(item)

    if not valid:
        return None

    valid.sort(key=lambda item: (item["score"], -abs(item["adverse_roi"])), reverse=True)
    best = valid[0]

    if best["score"] < get_config_float("LONG_TERM_SR_MIN_SCORE", 2.5):
        return None

    return best


def validate_adverse_zone_level(side, entry_price, trend_df, confirm_df, leverage=None):
    level = find_adverse_zone_level(
        side,
        entry_price,
        trend_df,
        confirm_df,
        leverage=leverage
    )

    if level:
        return True, level

    zone_roi = get_config_float("LONG_TERM_MAX_ADVERSE_ROI", 50)
    label = "support" if side == "BUY" else "resistance"
    return False, {
        "reason": f"NO STRONG {label.upper()} WITHIN -{zone_roi:.0f}% ROI ZONE"
    }


def _trend_bias_score(side, trend_df):
    trend = latest_closed(trend_df)
    prev = previous_closed(trend_df)
    structure = detect_market_structure(trend_df)
    min_adx = get_config_float("LONG_TERM_MIN_ADX", 14)
    score = 0
    hard_ok = False

    if side == "BUY":
        score = add_score(score, trend["close"] > trend["ema200"], 3)
        score = add_score(score, trend["ema50"] > trend["ema200"], 3)
        score = add_score(score, trend["ema20"] > trend["ema50"], 2)
        score = add_score(score, trend["close"] > trend["ema50"], 2)
        score = add_score(score, trend["ema20"] > prev["ema20"], 1)
        score = add_score(score, structure["bullish_structure"], 2)
        score = add_score(score, structure["bullish_breakout"], 2)
        score = add_score(score, trend["adx"] >= min_adx, 1)
        hard_ok = (
            trend["close"] > trend["ema50"]
            and (trend["ema20"] > trend["ema50"] or trend["ema50"] > trend["ema200"])
        )
    else:
        score = add_score(score, trend["close"] < trend["ema200"], 3)
        score = add_score(score, trend["ema50"] < trend["ema200"], 3)
        score = add_score(score, trend["ema20"] < trend["ema50"], 2)
        score = add_score(score, trend["close"] < trend["ema50"], 2)
        score = add_score(score, trend["ema20"] < prev["ema20"], 1)
        score = add_score(score, structure["bearish_structure"], 2)
        score = add_score(score, structure["bearish_breakdown"], 2)
        score = add_score(score, trend["adx"] >= min_adx, 1)
        hard_ok = (
            trend["close"] < trend["ema50"]
            and (trend["ema20"] < trend["ema50"] or trend["ema50"] < trend["ema200"])
        )

    return score, hard_ok


def _confirmation_score(side, confirm_df):
    confirm = latest_closed(confirm_df)
    prev = previous_closed(confirm_df)
    structure = detect_market_structure(confirm_df)
    min_adx = get_config_float("LONG_TERM_MIN_ADX", 14)
    score = 0
    hard_ok = False

    if side == "BUY":
        score = add_score(score, confirm["close"] > confirm["ema50"], 3)
        score = add_score(score, confirm["ema20"] > confirm["ema50"], 2)
        score = add_score(score, confirm["macd"] > confirm["macd_signal"], 2)
        score = add_score(score, 45 <= confirm["rsi"] <= 72, 2)
        score = add_score(score, confirm["adx"] >= min_adx, 1)
        score = add_score(score, structure["bullish_breakout"], 2)
        score = add_score(score, confirm["close"] > prev["high"], 1)
        score = add_score(score, confirm["volume"] > confirm["volume_sma"], 1)
        hard_ok = (
            confirm["close"] > confirm["ema20"]
            and (confirm["macd"] > confirm["macd_signal"] or confirm["rsi"] > 50)
        )
    else:
        score = add_score(score, confirm["close"] < confirm["ema50"], 3)
        score = add_score(score, confirm["ema20"] < confirm["ema50"], 2)
        score = add_score(score, confirm["macd"] < confirm["macd_signal"], 2)
        score = add_score(score, 28 <= confirm["rsi"] <= 55, 2)
        score = add_score(score, confirm["adx"] >= min_adx, 1)
        score = add_score(score, structure["bearish_breakdown"], 2)
        score = add_score(score, confirm["close"] < prev["low"], 1)
        score = add_score(score, confirm["volume"] > confirm["volume_sma"], 1)
        hard_ok = (
            confirm["close"] < confirm["ema20"]
            and (confirm["macd"] < confirm["macd_signal"] or confirm["rsi"] < 50)
        )

    return score, hard_ok


def _entry_score(side, entry_df):
    entry = latest_closed(entry_df)
    prev = previous_closed(entry_df)
    ema_distance = pct_distance(entry["close"], entry["ema20"])
    max_ema_distance = get_config_float("LONG_TERM_ENTRY_MAX_EMA_DISTANCE_PCT", 6)
    score = 0
    hard_ok = False

    if side == "BUY":
        bullish_candle = entry["close"] > entry["open"]
        score = add_score(score, entry["close"] > entry["ema20"], 2)
        score = add_score(score, entry["macd"] > entry["macd_signal"], 1)
        score = add_score(score, entry["rsi"] > 50, 1)
        score = add_score(score, bullish_candle, 1)
        score = add_score(score, entry["close"] > prev["high"], 1)
        score = add_score(score, ema_distance <= max_ema_distance, 1)
        score = add_score(score, entry["volume"] > entry["volume_sma"], 1)
        hard_ok = entry["close"] > entry["ema20"] and ema_distance <= max_ema_distance
    else:
        bearish_candle = entry["close"] < entry["open"]
        score = add_score(score, entry["close"] < entry["ema20"], 2)
        score = add_score(score, entry["macd"] < entry["macd_signal"], 1)
        score = add_score(score, entry["rsi"] < 50, 1)
        score = add_score(score, bearish_candle, 1)
        score = add_score(score, entry["close"] < prev["low"], 1)
        score = add_score(score, ema_distance <= max_ema_distance, 1)
        score = add_score(score, entry["volume"] > entry["volume_sma"], 1)
        hard_ok = entry["close"] < entry["ema20"] and ema_distance <= max_ema_distance

    return score, hard_ok, ema_distance


def _btc_context_score(side, btc_trend, btc_corr, rs):
    score = 0
    corr_threshold = get_config_float("LONG_TERM_BTC_CORR_THRESHOLD", 0.65)

    if btc_corr is not None and btc_corr >= corr_threshold:
        if side == "BUY":
            score = add_score(score, btc_trend == "BULLISH", 2)
            score -= 2 if btc_trend == "BEARISH" else 0
        else:
            score = add_score(score, btc_trend == "BEARISH", 2)
            score -= 2 if btc_trend == "BULLISH" else 0

    if rs is not None:
        if side == "BUY":
            score = add_score(score, rs > 1, 1)
            score -= 1 if rs < -2 else 0
        else:
            score = add_score(score, rs < -1, 1)
            score -= 1 if rs > 2 else 0

    return score


def _futures_participation_score(side, participation):
    if not participation or not participation.get("available"):
        return 0

    score = 0
    oi_change = participation.get("oi_change_pct")
    taker_ratio = participation.get("taker_buy_sell_ratio")
    global_ratio = participation.get("global_long_short_ratio")
    top_ratio = participation.get("top_long_short_ratio")
    funding_rate = participation.get("funding_rate")
    oi_min = get_config_float("FUTURES_CONTEXT_OI_MIN_CHANGE_PCT", 1.0)
    taker_buy_min = get_config_float("FUTURES_CONTEXT_TAKER_BUY_MIN", 1.05)
    taker_sell_max = get_config_float("FUTURES_CONTEXT_TAKER_SELL_MAX", 0.95)
    crowd_long_max = get_config_float("FUTURES_CONTEXT_CROWD_LONG_MAX", 2.2)
    crowd_short_min = get_config_float("FUTURES_CONTEXT_CROWD_SHORT_MIN", 0.45)
    funding_abs_max = get_config_float("FUTURES_CONTEXT_FUNDING_ABS_MAX", 0.001)

    if oi_change is not None:
        score = add_score(score, oi_change >= oi_min, 1.5)
        score -= 1 if oi_change <= -oi_min else 0

    if taker_ratio is not None:
        if side == "BUY":
            score = add_score(score, taker_ratio >= taker_buy_min, 2)
            score -= 2 if taker_ratio <= taker_sell_max else 0
        else:
            score = add_score(score, taker_ratio <= taker_sell_max, 2)
            score -= 2 if taker_ratio >= taker_buy_min else 0

    crowd_ratio = top_ratio if top_ratio is not None else global_ratio

    if crowd_ratio is not None:
        if side == "BUY":
            score -= 1.5 if crowd_ratio >= crowd_long_max else 0
            score = add_score(score, crowd_ratio <= 1, 0.5)
        else:
            score -= 1.5 if crowd_ratio <= crowd_short_min else 0
            score = add_score(score, crowd_ratio >= 1, 0.5)

    if funding_rate is not None:
        if side == "BUY":
            score -= 1 if funding_rate >= funding_abs_max else 0
            score = add_score(score, funding_rate <= -funding_abs_max, 0.5)
        else:
            score -= 1 if funding_rate <= -funding_abs_max else 0
            score = add_score(score, funding_rate >= funding_abs_max, 0.5)

    return round(score, 2)


def _side_signal_score(
    side,
    trend_df,
    confirm_df,
    entry_df,
    btc_trend,
    btc_corr,
    rs,
    participation=None
):
    entry_price = latest_closed(entry_df)["close"]
    level_ok, level = validate_adverse_zone_level(
        side,
        entry_price,
        trend_df,
        confirm_df
    )
    trend_score, trend_ok = _trend_bias_score(side, trend_df)
    confirm_score, confirm_ok = _confirmation_score(side, confirm_df)
    entry_score, entry_ok, ema_distance = _entry_score(side, entry_df)
    btc_score = _btc_context_score(side, btc_trend, btc_corr, rs)
    participation_score = _futures_participation_score(side, participation)
    level_score = 4 if level_ok else 0

    total = (
        trend_score +
        confirm_score +
        entry_score +
        btc_score +
        level_score +
        participation_score
    )
    hard_ok = trend_ok and confirm_ok and entry_ok and level_ok

    return {
        "side": side,
        "score": max(0, total),
        "confidence": score_to_confidence(max(0, total)),
        "base_score": trend_score + confirm_score + entry_score + btc_score + level_score,
        "trend_score": trend_score,
        "confirm_score": confirm_score,
        "entry_score": entry_score,
        "btc_score": btc_score,
        "level_score": level_score,
        "participation_score": participation_score,
        "hard_ok": hard_ok,
        "trend_ok": trend_ok,
        "confirm_ok": confirm_ok,
        "entry_ok": entry_ok,
        "level_ok": level_ok,
        "level": level,
        "ema_distance": ema_distance,
    }


def _select_signal(buy, sell):
    threshold = config.LONG_TERM_SIGNAL_THRESHOLD
    min_edge = config.LONG_TERM_MIN_SIGNAL_EDGE

    if (
        buy["hard_ok"]
        and buy["confidence"] >= threshold
        and buy["confidence"] >= sell["confidence"] + min_edge
    ):
        return "BUY"

    if (
        sell["hard_ok"]
        and sell["confidence"] >= threshold
        and sell["confidence"] >= buy["confidence"] + min_edge
    ):
        return "SELL"

    return None


def log_signal_analysis(analysis):
    buy = analysis["buy"]
    sell = analysis["sell"]

    log_info(
        f"BUY conf={buy.get('confidence', 0)}% hard={buy.get('hard_ok', False)} "
        f"level={buy.get('level_ok', False)} "
        f"futures={buy.get('participation_score', 0)} | "
        f"SELL conf={sell.get('confidence', 0)}% "
        f"hard={sell.get('hard_ok', False)} "
        f"level={sell.get('level_ok', False)} "
        f"futures={sell.get('participation_score', 0)}"
    )

    if buy.get("level_ok"):
        log_info(
            f"BUY support {buy.get('level', {}).get('level')} "
            f"ROI={buy.get('level', {}).get('adverse_roi')}% "
            f"SRC={buy.get('level', {}).get('source')}"
        )
    else:
        log_warning(
            f"BUY BLOCKED | {buy.get('level', {}).get('reason', 'NO DETAILS')}"
        )

    if sell.get("level_ok"):
        log_info(
            f"SELL resistance {sell.get('level', {}).get('level')} "
            f"ROI={sell.get('level', {}).get('adverse_roi')}% "
            f"SRC={sell.get('level', {}).get('source')}"
        )
    else:
        log_warning(
            f"SELL BLOCKED | {sell.get('level', {}).get('reason', 'NO DETAILS')}"
        )

    if analysis["signal"]:
        log_info(
            f"FINAL LONG-TERM {analysis['signal']} "
            f"CONFIDENCE: "
            f"{analysis[analysis['signal'].lower()].get('confidence', 0)}"
        )


def analyze_signal(
    trend_df,
    confirm_df,
    entry_df,
    btc_trend,
    btc_corr,
    rs,
    participation=None,
    log_details=True
):
    try:
        buy = _side_signal_score(
            "BUY",
            trend_df,
            confirm_df,
            entry_df,
            btc_trend,
            btc_corr,
            rs,
            participation=participation
        )
        sell = _side_signal_score(
            "SELL",
            trend_df,
            confirm_df,
            entry_df,
            btc_trend,
            btc_corr,
            rs,
            participation=participation
        )
        signal = _select_signal(buy, sell)
        best = buy if buy["confidence"] >= sell["confidence"] else sell
        analysis = {
            "buy": buy,
            "sell": sell,
            "signal": signal,
            "best_side": best["side"],
            "best_confidence": best["confidence"],
            "threshold": config.LONG_TERM_SIGNAL_THRESHOLD,
            "min_edge": config.LONG_TERM_MIN_SIGNAL_EDGE,
            "participation_available": bool(
                participation and participation.get("available")
            ),
        }

        if log_details:
            log_signal_analysis(analysis)

        return analysis

    except Exception as e:
        log_error(f"STRATEGY ERROR: {e}")
        return {
            "buy": {},
            "sell": {},
            "signal": None,
            "best_side": None,
            "best_confidence": 0,
            "threshold": config.LONG_TERM_SIGNAL_THRESHOLD,
            "min_edge": config.LONG_TERM_MIN_SIGNAL_EDGE,
            "participation_available": False,
            "error": str(e),
        }


def should_fetch_futures_context(analysis):
    if not config.FUTURES_CONTEXT_ENABLED:
        return False

    if analysis.get("best_confidence", 0) < config.FUTURES_CONTEXT_MIN_CONFIDENCE:
        return False

    buy = analysis.get("buy", {})
    sell = analysis.get("sell", {})

    return bool(buy.get("level_ok") or sell.get("level_ok"))


def check_signal(trend_df, confirm_df, entry_df, btc_trend, btc_corr, rs):
    return analyze_signal(
        trend_df,
        confirm_df,
        entry_df,
        btc_trend,
        btc_corr,
        rs,
        log_details=True
    )["signal"]
