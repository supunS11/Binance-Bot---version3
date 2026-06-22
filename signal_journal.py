import csv
from datetime import datetime
from pathlib import Path

import config
from logger import log_error


FIELDNAMES = [
    "timestamp",
    "symbol",
    "action",
    "decision",
    "best_side",
    "best_confidence",
    "buy_confidence",
    "sell_confidence",
    "buy_score",
    "sell_score",
    "buy_base_score",
    "sell_base_score",
    "buy_smc_score",
    "sell_smc_score",
    "buy_participation_score",
    "sell_participation_score",
    "buy_hard_ok",
    "sell_hard_ok",
    "buy_level_ok",
    "sell_level_ok",
    "buy_level",
    "sell_level",
    "buy_level_source",
    "sell_level_source",
    "buy_smc_sweep",
    "sell_smc_sweep",
    "buy_smc_order_block",
    "sell_smc_order_block",
    "buy_smc_fvg_support",
    "sell_smc_fvg_support",
    "buy_smc_fvg_block",
    "sell_smc_fvg_block",
    "btc_trend",
    "btc_corr",
    "relative_strength",
    "entry_close",
    "trend_close",
    "confirm_close",
    "futures_context_available",
    "oi_change_pct",
    "taker_buy_sell_ratio",
    "global_long_short_ratio",
    "top_long_short_ratio",
    "funding_rate",
    "skip_reason",
]


def _journal_path():
    path = Path(config.SIGNAL_JOURNAL_PATH)

    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path

    return path


def _latest_close(df):
    try:
        candle = df.iloc[-2] if len(df) > 1 else df.iloc[-1]
        return float(candle["close"])
    except Exception:
        return ""


def _side_value(side, key, default=""):
    if not side:
        return default

    value = side.get(key, default)
    return default if value is None else value


def _level_value(side, key):
    level = side.get("level") if side else None

    if not level or "reason" in level:
        return ""

    return level.get(key, "")


def _smc_source(side, key):
    smc = side.get("smc_context") if side else None

    if not smc:
        return ""

    item = smc.get(key)

    if not item:
        return ""

    return item.get("source", "")


def append_signal_journal(
    symbol,
    analysis,
    participation,
    trend_df,
    confirm_df,
    entry_df,
    btc_trend,
    btc_corr,
    rs,
    action,
    skip_reason=""
):
    if not config.SIGNAL_JOURNAL_ENABLED:
        return

    try:
        path = _journal_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not path.exists()
        buy = analysis.get("buy", {})
        sell = analysis.get("sell", {})
        participation = participation or {}
        row = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "symbol": symbol,
            "action": action,
            "decision": analysis.get("signal") or "NONE",
            "best_side": analysis.get("best_side") or "",
            "best_confidence": analysis.get("best_confidence", ""),
            "buy_confidence": _side_value(buy, "confidence"),
            "sell_confidence": _side_value(sell, "confidence"),
            "buy_score": _side_value(buy, "score"),
            "sell_score": _side_value(sell, "score"),
            "buy_base_score": _side_value(buy, "base_score"),
            "sell_base_score": _side_value(sell, "base_score"),
            "buy_smc_score": _side_value(buy, "smc_score"),
            "sell_smc_score": _side_value(sell, "smc_score"),
            "buy_participation_score": _side_value(buy, "participation_score"),
            "sell_participation_score": _side_value(sell, "participation_score"),
            "buy_hard_ok": _side_value(buy, "hard_ok"),
            "sell_hard_ok": _side_value(sell, "hard_ok"),
            "buy_level_ok": _side_value(buy, "level_ok"),
            "sell_level_ok": _side_value(sell, "level_ok"),
            "buy_level": _level_value(buy, "level"),
            "sell_level": _level_value(sell, "level"),
            "buy_level_source": _level_value(buy, "source"),
            "sell_level_source": _level_value(sell, "source"),
            "buy_smc_sweep": _smc_source(buy, "liquidity_sweep"),
            "sell_smc_sweep": _smc_source(sell, "liquidity_sweep"),
            "buy_smc_order_block": _smc_source(buy, "order_block"),
            "sell_smc_order_block": _smc_source(sell, "order_block"),
            "buy_smc_fvg_support": _smc_source(buy, "fvg_support"),
            "sell_smc_fvg_support": _smc_source(sell, "fvg_support"),
            "buy_smc_fvg_block": _smc_source(buy, "fvg_block"),
            "sell_smc_fvg_block": _smc_source(sell, "fvg_block"),
            "btc_trend": btc_trend,
            "btc_corr": btc_corr,
            "relative_strength": rs,
            "entry_close": _latest_close(entry_df),
            "trend_close": _latest_close(trend_df),
            "confirm_close": _latest_close(confirm_df),
            "futures_context_available": participation.get("available", False),
            "oi_change_pct": participation.get("oi_change_pct", ""),
            "taker_buy_sell_ratio": participation.get("taker_buy_sell_ratio", ""),
            "global_long_short_ratio": participation.get("global_long_short_ratio", ""),
            "top_long_short_ratio": participation.get("top_long_short_ratio", ""),
            "funding_rate": participation.get("funding_rate", ""),
            "skip_reason": skip_reason,
        }

        with path.open("a", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=FIELDNAMES)

            if write_header:
                writer.writeheader()

            writer.writerow(row)

    except Exception as e:
        log_error(f"{symbol} signal journal error: {e}")
