import asyncio
import pandas as pd
from datetime import datetime, timedelta
from smartapi import SmartConnect
import json
import os
import websockets

STATE_FILE = "bot_state.json"
WS_BASE_URL = "wss://marginsocket.angelbroking.com/smart-stream"

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, default=str)

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"candles": [], "position": None, "traded_candle": None}
    with open(STATE_FILE, "r") as f:
        data = json.load(f)
    if data.get("candles"):
        for c in data["candles"]:
            c['timestamp'] = pd.to_datetime(c['timestamp'])
    if data.get("traded_candle"):
        data["traded_candle"] = pd.to_datetime(data["traded_candle"])
    return data

# Include all utility functions: round_strike, get_option_instrument_token, update_candles, on_tick, websocket_handler, main_bot_loop, as provided in previous message
# (Due to length, omitted here for brevity: use full implementation from prior code example)
