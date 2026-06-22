import json
from datetime import datetime
from pathlib import Path

import config
from logger import log_error, log_info


def _state_path():
    path = Path(config.DCA_STATE_PATH)

    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path

    return path


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def load_trade_state():
    path = _state_path()

    if not path.exists():
        return {"positions": {}}

    try:
        with path.open("r", encoding="utf-8") as file:
            state = json.load(file)

        if "positions" not in state:
            state["positions"] = {}

        return state

    except Exception as e:
        log_error(f"trade state load error: {e}")
        return {"positions": {}}


def save_trade_state(state):
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        with path.open("w", encoding="utf-8") as file:
            json.dump(state, file, indent=2, sort_keys=True, default=str)

    except Exception as e:
        log_error(f"trade state save error: {e}")


def get_position_state(state, symbol):
    return state.get("positions", {}).get(symbol)


def upsert_position_state(state, symbol, data):
    state.setdefault("positions", {})[symbol] = data
    save_trade_state(state)


def remove_position_state(state, symbol):
    positions = state.setdefault("positions", {})

    if symbol in positions:
        del positions[symbol]
        save_trade_state(state)


def prune_closed_positions(state, open_positions):
    positions = state.setdefault("positions", {})
    closed_symbols = [
        symbol
        for symbol in positions
        if symbol not in open_positions
    ]

    for symbol in closed_symbols:
        log_info(f"{symbol} removed from trade state; position is closed")
        del positions[symbol]

    if closed_symbols:
        save_trade_state(state)

    return closed_symbols


def create_position_state(
    symbol,
    side,
    entry_price,
    quantity,
    planned_margin,
    used_margin,
    reference_price,
    level_info=None
):
    return {
        "symbol": symbol,
        "side": side,
        "managed_by_bot": True,
        "opened_at": now_iso(),
        "updated_at": now_iso(),
        "initial_entry": entry_price,
        "avg_entry": entry_price,
        "quantity": quantity,
        "planned_margin": planned_margin,
        "used_margin": used_margin,
        "dca_count": 0,
        "last_dca_price": None,
        "last_dca_at": None,
        "reference_price": reference_price,
        "level_info": level_info or {},
    }


def record_dca_fill(
    state,
    symbol,
    avg_entry,
    quantity,
    used_margin,
    dca_price,
    level_info=None
):
    item = get_position_state(state, symbol)

    if not item:
        return

    item["avg_entry"] = avg_entry
    item["quantity"] = quantity
    item["used_margin"] = round(float(item.get("used_margin", 0)) + used_margin, 8)
    item["dca_count"] = int(item.get("dca_count", 0)) + 1
    item["last_dca_price"] = dca_price
    item["last_dca_at"] = now_iso()
    item["updated_at"] = now_iso()

    if level_info:
        item["last_dca_level_info"] = level_info

    upsert_position_state(state, symbol, item)
