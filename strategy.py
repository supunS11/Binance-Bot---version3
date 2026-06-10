import config
from logger import log_info, log_error
from ai_model import ai_confidence_boost


def score_to_confidence(score, max_score=24):

    if score <= 0:
        return 0

    confidence = (score / max_score) * 100
    confidence = confidence ** 1.15

    return round(min(confidence, 100), 2)


# =========================================================
# STRUCTURE-BASED STOP LOSS (NEW - SAFE ADDITION)
# =========================================================
def get_structure_stop_loss(df, side):

    try:
        atr = df['atr'].iloc[-1]

        if side == "BUY":

            swing_low = df['low'].iloc[-10:-1].min()
            return swing_low - (atr * 0.5)

        else:

            swing_high = df['high'].iloc[-10:-1].max()
            return swing_high + (atr * 0.5)

    except Exception as e:
        log_error(f"STRUCTURE SL ERROR: {e}")
        return None


# =========================================================
# LIQUIDITY SWEEP DETECTION (UNCHANGED)
# =========================================================
def detect_liquidity_sweep(df):

    try:

        prev_high = df['high'].iloc[-3]
        prev_low = df['low'].iloc[-3]

        last_high = df['high'].iloc[-1]
        last_low = df['low'].iloc[-1]
        close = df['close'].iloc[-1]

        bullish_sweep = (
            last_low < prev_low and
            close > prev_low
        )

        bearish_sweep = (
            last_high > prev_high and
            close < prev_high
        )

        return bullish_sweep, bearish_sweep

    except Exception:
        return False, False


# =========================================================
# ORDER BLOCK DETECTION (UNCHANGED)
# =========================================================
def detect_order_block(df):

    try:

        body = abs(df['close'] - df['open'])

        idx = body.iloc[-20:].idxmax()

        ob_high = df['high'].loc[idx]
        ob_low = df['low'].loc[idx]

        ob_type = (
            "BULLISH"
            if df['close'].loc[idx] > df['open'].loc[idx]
            else "BEARISH"
        )

        return ob_high, ob_low, ob_type

    except Exception:
        return None, None, None


# =========================================================
# MAIN SIGNAL ENGINE (UNCHANGED LOGIC)
# =========================================================
def check_signal(trend_df, confirm_df, entry_df, btc_trend, btc_corr, rs):

    try:

        trend = trend_df.iloc[-2]
        confirm = confirm_df.iloc[-2]
        entry = entry_df.iloc[-2]

        support = trend_df['low'].rolling(50).min().iloc[-1]
        resistance = trend_df['high'].rolling(50).max().iloc[-1]
        price = trend_df['close'].iloc[-1]

        bullish_sweep, bearish_sweep = detect_liquidity_sweep(confirm_df)
        ob_high, ob_low, ob_type = detect_order_block(confirm_df)

        atr_pct = (entry['atr'] / entry['close']) * 100

        log_info(f"ATR%: {round(atr_pct, 2)}")

        # ======================
        # ATR FILTER
        # ======================
        if atr_pct < 0.2 or atr_pct > 3.0:
            log_info(f"ATR FILTER BLOCKED | ATR%: {round(atr_pct, 2)}")
            return None

        ema_gap_pct = ((trend['ema50'] - trend['ema200']) / trend['ema200']) * 100

        # ======================
        # REGIME
        # ======================
        regime = "NORMAL"

        if confirm['adx'] > 25 and abs(ema_gap_pct) > 1:
            regime = "TRENDING"

        elif confirm['adx'] < 18 or abs(ema_gap_pct) < 0.3:
            regime = "SIDEWAYS"

        log_info(f"MARKET REGIME: {regime}")

        # ======================
        # BUY SCORE
        # ======================
        buy_score = 0

        resistance_distance = ((resistance - price) / price) * 100

        bullish_ema_rejection = all(
            trend_df['low'].iloc[-i] > trend_df['ema50'].iloc[-i]
            for i in range(1, 4)
        )

        required_distance = (
            config.ROI_PERCENT_TP /
            config.LEVERAGE
        ) + 0.7

        if (
            trend['ema50'] > trend['ema200'] and
            bullish_ema_rejection and
            trend['close'] > trend['ema50'] and
            0.5 < ema_gap_pct < 8 and
            resistance_distance > required_distance
        ):
            buy_score += 2

        if confirm['macd'] > confirm['macd_signal']:
            buy_score += 1

        if 52 < confirm['rsi'] < 72:
            buy_score += 1

        if confirm['adx'] > 20:
            buy_score += 1

        if entry['close'] > entry['ema20']:
            buy_score += 1

        if abs(entry['close'] - entry['ema20']) / entry['ema20'] < 0.01:
            buy_score += 1

        if entry['volume'] > entry['volume_sma'] * 1.1:
            buy_score += 1

        if entry['close'] > entry['open']:
            buy_score += 1

        if btc_corr >= 0.75:
            if btc_trend == "BULLISH":
                buy_score += 2
            elif btc_trend == "BEARISH":
                buy_score -= 2

        if rs > 2:
            buy_score += 2

        if bullish_sweep:
            buy_score += 2

        if ob_type == "BULLISH" and ob_low <= price <= ob_high:
            buy_score += 2

        # ======================
        # REVERSAL BUY
        # ======================
        recent_high = trend_df['high'].iloc[-20:-5].max()

        if trend_df['close'].iloc[-1] > recent_high:
            buy_score += 2

        bullish_macd_cross = (
            confirm_df['macd'].iloc[-3] <= confirm_df['macd_signal'].iloc[-3]
            and confirm_df['macd'].iloc[-2] > confirm_df['macd_signal'].iloc[-2]
        )

        bullish_rsi_cross = (
            confirm_df['rsi'].iloc[-3] < 50
            and confirm_df['rsi'].iloc[-2] > 50
        )

        if bullish_macd_cross:
            buy_score += 1

        if bullish_rsi_cross:
            buy_score += 1

        # ======================
        # REGIME ADJUSTMENT
        # ======================
        if regime == "TRENDING":
            buy_score += 1
        elif regime == "SIDEWAYS":
            buy_score -= 3

        # ======================
        # SELL SCORE
        # ======================
        sell_score = 0

        support_distance = ((price - support) / price) * 100

        bearish_ema_rejection = all(
            trend_df['high'].iloc[-i] < trend_df['ema50'].iloc[-i]
            for i in range(1, 4)
        )

        if (
            trend['ema50'] < trend['ema200'] and
            bearish_ema_rejection and
            trend['close'] < trend['ema50'] and
            -8 < ema_gap_pct < -0.5 and
            support_distance > required_distance
        ):
            sell_score += 2

        if confirm['macd'] < confirm['macd_signal']:
            sell_score += 1

        if 28 < confirm['rsi'] < 48:
            sell_score += 1

        if confirm['adx'] > 20:
            sell_score += 1

        if entry['close'] < entry['ema20']:
            sell_score += 1

        if abs(entry['close'] - entry['ema20']) / entry['ema20'] < 0.01:
            sell_score += 1

        if entry['volume'] > entry['volume_sma'] * 1.1:
            sell_score += 1

        if entry['close'] < entry['open']:
            sell_score += 1

        if btc_corr >= 0.75:
            if btc_trend == "BEARISH":
                sell_score += 2
            elif btc_trend == "BULLISH":
                sell_score -= 2

        if rs < -2:
            sell_score += 2

        if bearish_sweep:
            sell_score += 2

        if ob_type == "BEARISH" and ob_low <= price <= ob_high:
            sell_score += 2

        # ======================
        # REVERSAL SELL
        # ======================
        recent_low = trend_df['low'].iloc[-20:-5].min()

        if trend_df['close'].iloc[-1] < recent_low:
            sell_score += 2

        bearish_macd_cross = (
            confirm_df['macd'].iloc[-3] >= confirm_df['macd_signal'].iloc[-3]
            and confirm_df['macd'].iloc[-2] < confirm_df['macd_signal'].iloc[-2]
        )

        bearish_rsi_cross = (
            confirm_df['rsi'].iloc[-3] > 50
            and confirm_df['rsi'].iloc[-2] < 50
        )

        if bearish_macd_cross:
            sell_score += 1

        if bearish_rsi_cross:
            sell_score += 1

        # ======================
        # REGIME ADJUSTMENT
        # ======================
        if regime == "TRENDING":
            sell_score += 1
        elif regime == "SIDEWAYS":
            sell_score -= 3

        # ======================
        # FINAL SCORES
        # ======================
        buy_score = max(0, buy_score)
        sell_score = max(0, sell_score)

        buy_conf = score_to_confidence(buy_score)
        sell_conf = score_to_confidence(sell_score)

        log_info(f"BUY conf: {buy_conf}% | SELL conf: {sell_conf}%")

        signal_guess = "BUY" if buy_conf > sell_conf else "SELL"

        ai_boost = ai_confidence_boost(trend_df, confirm_df, entry_df, signal_guess)

        if buy_conf > sell_conf:
            buy_conf = min(100, max(0, buy_conf + ai_boost))
        else:
            sell_conf = min(100, max(0, sell_conf + ai_boost))

        # ======================
        # FINAL DECISION
        # ======================
        if buy_conf >= 80 and buy_conf > sell_conf:
            log_info(f"FINAL BUY CONFIDENCE: {buy_conf}")
            return "BUY"

        if sell_conf >= 80 and sell_conf > buy_conf:
            log_info(f"FINAL SELL CONFIDENCE: {sell_conf}")
            return "SELL"

        return None

    except Exception as e:
        log_error(f"STRATEGY ERROR: {e}")
        return None