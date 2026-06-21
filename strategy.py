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


def _dedupe_levels(levels, tolerance):
    deduped = []

    for level in sorted(levels, key=lambda item: item["score"], reverse=True):
        if any(abs(level["level"] - item["level"]) <= tolerance for item in deduped):
            continue

        deduped.append(level)

    return deduped


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


def _side_signal_score(side, trend_df, confirm_df, entry_df, btc_trend, btc_corr, rs):
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
    level_score = 4 if level_ok else 0

    total = trend_score + confirm_score + entry_score + btc_score + level_score
    hard_ok = trend_ok and confirm_ok and entry_ok and level_ok

    return {
        "side": side,
        "score": max(0, total),
        "confidence": score_to_confidence(max(0, total)),
        "hard_ok": hard_ok,
        "trend_ok": trend_ok,
        "confirm_ok": confirm_ok,
        "entry_ok": entry_ok,
        "level_ok": level_ok,
        "level": level,
        "ema_distance": ema_distance,
    }


def check_signal(trend_df, confirm_df, entry_df, btc_trend, btc_corr, rs):
    try:
        buy = _side_signal_score(
            "BUY",
            trend_df,
            confirm_df,
            entry_df,
            btc_trend,
            btc_corr,
            rs
        )
        sell = _side_signal_score(
            "SELL",
            trend_df,
            confirm_df,
            entry_df,
            btc_trend,
            btc_corr,
            rs
        )
        threshold = config.LONG_TERM_SIGNAL_THRESHOLD
        min_edge = config.LONG_TERM_MIN_SIGNAL_EDGE

        log_info(
            f"BUY conf={buy['confidence']}% hard={buy['hard_ok']} "
            f"level={buy['level_ok']} | "
            f"SELL conf={sell['confidence']}% hard={sell['hard_ok']} "
            f"level={sell['level_ok']}"
        )

        if buy["level_ok"]:
            log_info(
                f"BUY support {buy['level']['level']} "
                f"ROI={buy['level']['adverse_roi']}% "
                f"SRC={buy['level']['source']}"
            )
        else:
            log_warning(f"BUY BLOCKED | {buy['level']['reason']}")

        if sell["level_ok"]:
            log_info(
                f"SELL resistance {sell['level']['level']} "
                f"ROI={sell['level']['adverse_roi']}% "
                f"SRC={sell['level']['source']}"
            )
        else:
            log_warning(f"SELL BLOCKED | {sell['level']['reason']}")

        if (
            buy["hard_ok"]
            and buy["confidence"] >= threshold
            and buy["confidence"] >= sell["confidence"] + min_edge
        ):
            log_info(f"FINAL LONG-TERM BUY CONFIDENCE: {buy['confidence']}")
            return "BUY"

        if (
            sell["hard_ok"]
            and sell["confidence"] >= threshold
            and sell["confidence"] >= buy["confidence"] + min_edge
        ):
            log_info(f"FINAL LONG-TERM SELL CONFIDENCE: {sell['confidence']}")
            return "SELL"

        return None

    except Exception as e:
        log_error(f"STRATEGY ERROR: {e}")
        return None
