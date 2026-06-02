from logger import log_info, log_error
from ai_model import ai_confidence_boost


def score_to_confidence(score, max_score=15):

    if score <= 0:
        return 0

    confidence = (score / max_score) * 100
    confidence = confidence ** 1.15

    return round(min(confidence, 100), 2)


def check_signal(trend_df, confirm_df, entry_df, btc_trend, btc_corr, rs):

    try:

        trend = trend_df.iloc[-2]
        confirm = confirm_df.iloc[-2]
        entry = entry_df.iloc[-2]

        support = trend_df['low'].rolling(50).min().iloc[-1]
        resistance = trend_df['high'].rolling(50).max().iloc[-1]
        price = trend_df['close'].iloc[-1]

        atr_pct = (
            entry['atr']
            / entry['close']
        ) * 100

        log_info(
            f"ATR%: {round(atr_pct, 2)}"
        )

        # ======================
        # ATR VOLATILITY FILTER
        # ======================

        if atr_pct < 0.2 or atr_pct > 3.0:

            log_info(
                f"ATR FILTER BLOCKED | ATR%: {round(atr_pct, 2)}"
            )

            return None

        ema_gap_pct = (
            (trend['ema50'] - trend['ema200'])
            / trend['ema200']
        ) * 100

        # ======================
        # MARKET REGIME FILTER
        # ======================
        regime = "NORMAL"

        if (
            confirm['adx'] > 25 and
            abs(ema_gap_pct) > 1
        ):
            regime = "TRENDING"

        elif (
            confirm['adx'] < 18 or
            abs(ema_gap_pct) < 0.3
        ):
            regime = "SIDEWAYS"

        log_info(f"MARKET REGIME: {regime}")

        # ======================
        # BUY SCORE
        # ======================
        buy_score = 0

        resistance_distance = (
            (resistance - price)
            / price
        ) * 100

        bullish_ema_rejection = all(
            trend_df['low'].iloc[-i] >
            trend_df['ema50'].iloc[-i]
            for i in range(1, 4)
        )

        if (
            trend_df['ema50'].iloc[-1] >
            trend_df['ema200'].iloc[-1] and
            bullish_ema_rejection and
            trend_df['close'].iloc[-1] >
            trend_df['ema50'].iloc[-1] and
            0.5 < ema_gap_pct < 8 and
            resistance_distance > 2
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

        if (
            abs(
                entry['close']
                - entry['ema20']
            ) / entry['ema20']
        ) < 0.01:
            buy_score += 1

        if (
            entry['volume'] >
            entry['volume_sma'] * 1.1
        ):
            buy_score += 1

        if entry['close'] > entry['open']:
            buy_score += 1

        if btc_corr >= 0.60:

            if btc_trend == "BULLISH":
                buy_score += 2

            elif btc_trend == "BEARISH":
                buy_score -= 2

        if rs > 2:
            buy_score += 2

        # ======================
        # REVERSAL DETECTION BUY
        # ======================

        recent_high = (
            trend_df['high']
            .iloc[-20:-5]
            .max()
        )

        bullish_structure_break = (
            trend_df['close'].iloc[-1] >
            recent_high
        )

        bullish_macd_cross = (
            confirm_df['macd'].iloc[-3] <=
            confirm_df['macd_signal'].iloc[-3]
            and
            confirm_df['macd'].iloc[-2] >
            confirm_df['macd_signal'].iloc[-2]
        )

        bullish_rsi_cross = (
            confirm_df['rsi'].iloc[-2] < 50
            and
            confirm_df['rsi'].iloc[-1] > 50
        )

        if bullish_structure_break:
            buy_score += 2

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

        support_distance = (
            (price - support)
            / price
        ) * 100

        bearish_ema_rejection = all(
            trend_df['high'].iloc[-i] <
            trend_df['ema50'].iloc[-i]
            for i in range(1, 4)
        )

        if (
            trend_df['ema50'].iloc[-1] <
            trend_df['ema200'].iloc[-1] and
            bearish_ema_rejection and
            trend_df['close'].iloc[-1] <
            trend_df['ema50'].iloc[-1] and
            -8 < ema_gap_pct < -0.5 and
            support_distance > 2
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

        if (
            abs(
                entry['close']
                - entry['ema20']
            ) / entry['ema20']
        ) < 0.01:
            sell_score += 1

        if (
            entry['volume'] >
            entry['volume_sma'] * 1.1
        ):
            sell_score += 1

        if entry['close'] < entry['open']:
            sell_score += 1

        if btc_corr >= 0.60:

            if btc_trend == "BEARISH":
                sell_score += 2

            elif btc_trend == "BULLISH":
                sell_score -= 2

        if rs < -2:
            sell_score += 2

        # ======================
        # REVERSAL DETECTION SELL
        # ======================

        recent_low = (
            trend_df['low']
            .iloc[-20:-5]
            .min()
        )

        bearish_structure_break = (
            trend_df['close'].iloc[-1] <
            recent_low
        )

        bearish_macd_cross = (
            confirm_df['macd'].iloc[-3] >=
            confirm_df['macd_signal'].iloc[-3]
            and
            confirm_df['macd'].iloc[-2] <
            confirm_df['macd_signal'].iloc[-2]
        )

        bearish_rsi_cross = (
            confirm_df['rsi'].iloc[-2] > 50
            and
            confirm_df['rsi'].iloc[-1] < 50
        )

        if bearish_structure_break:
            sell_score += 2

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

        # Prevent negative scores
        buy_score = max(0, buy_score)
        sell_score = max(0, sell_score)

        # ======================
        # CONVERT TO CONFIDENCE
        # ======================
        buy_conf = score_to_confidence(buy_score)
        sell_conf = score_to_confidence(sell_score)

        log_info(
            f"BUY conf: {buy_conf}% | "
            f"SELL conf: {sell_conf}%"
        )

        # ======================
        # AI BOOST
        # ======================
        signal_guess = (
            "BUY"
            if buy_conf > sell_conf
            else "SELL"
        )

        ai_boost = ai_confidence_boost(
            trend_df,
            confirm_df,
            entry_df,
            signal_guess
        )

        if buy_conf > sell_conf:

            buy_conf = max(
                0,
                min(
                    100,
                    buy_conf + ai_boost
                )
            )

        elif sell_conf > buy_conf:

            sell_conf = max(
                0,
                min(
                    100,
                    sell_conf + ai_boost
                )
            )

        # ======================
        # FINAL DECISION
        # ======================
        if (
            buy_conf >= 80 and
            buy_conf > sell_conf
        ):

            log_info(
                f"FINAL BUY CONFIDENCE: "
                f"{buy_conf}"
            )

            return "BUY"

        if (
            sell_conf >= 80 and
            sell_conf > buy_conf
        ):

            log_info(
                f"FINAL SELL CONFIDENCE: "
                f"{sell_conf}"
            )

            return "SELL"

        return None

    except Exception as e:

        log_error(
            f"STRATEGY ERROR: {e}"
        )

        return None