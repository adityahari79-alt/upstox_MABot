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

def round_strike(price, interval=50):
    return int(price // interval * interval)

def get_option_instrument_token(strike, expiry_date, client: SmartConnect):
    try:
        instruments = client.searchInstruments(exchange="NFO", symbol="NIFTY")
    except Exception as e:
        print(f"Error fetching instruments: {e}")
        return None, None
    for inst in instruments:
        if ('expiry' not in inst or 'strikeprice' not in inst or 'optiontype' not in inst):
            continue
        if (inst['expiry'].strftime("%Y-%m-%d") == expiry_date and
                inst['strikeprice'] == strike and
                inst['optiontype'].upper() == 'CE'):
            return inst['symboltoken'], inst['tradingsymbol']
    return None, None

def update_candles(candles, ts, price, minutes=5):
    start = ts - timedelta(minutes=ts.minute % minutes, seconds=ts.second, microseconds=ts.microsecond)
    if not candles or candles[-1]['timestamp'] != pd.Timestamp(start):
        candles.append({"timestamp": pd.Timestamp(start), "open": price, "high": price, "low": price, "close": price})
    else:
        c = candles[-1]
        c['high'] = max(c['high'], price)
        c['low'] = min(c['low'], price)
        c['close'] = price
    return candles

async def on_tick(tick, state, client, expiry_date, lot_size, paper_mode):
    try:
        ts = datetime.fromtimestamp(tick['timestamp'] / 1000)
        ltp = float(tick['lastprice'])
    except Exception:
        return

    state['candles'] = update_candles(state.get('candles', []), ts, ltp)
    save_state(state)

    df = pd.DataFrame(state['candles'])
    if len(df) < 21:
        return

    df['ma10'] = df['close'].rolling(10).mean()
    df['ma21'] = df['close'].rolling(21).mean()
    last = df.iloc[-2]

    if (last['ma10'] >= last['ma21'] and state.get('traded_candle') != last['timestamp'] and not state.get('position')):
        strike = round_strike(last['close']) - 200
        opt_token, opt_symbol = get_option_instrument_token(strike, expiry_date, client)
        if not opt_token:
            print("Option instrument not found.")
            return

        if paper_mode:
            entry_price = last['close']
            print(f"[PAPER] Bought {strike} CE @ {entry_price}")
        else:
            order_params = {
                "variety": "NORMAL",
                "tradingsymbol": opt_symbol,
                "symboltoken": opt_token,
                "transactiontype": "BUY",
                "exchange": "NFO",
                "ordertype": "MARKET",
                "producttype": "INTRADAY",
                "duration": "DAY",
                "quantity": lot_size
            }
            try:
                order_response = client.placeOrder(order_params)
                entry_price = order_response['data']['averageprice']
                print(f"Bought {strike} CE @ {entry_price}")
            except Exception as e:
                print(f"Buy failed: {e}")
                return

        state['position'] = {
            'option_token': opt_token,
            'tradingsymbol': opt_symbol,
            'entry_price': entry_price,
            'sl_price': entry_price * 0.95,
            'max_price': entry_price
        }
        state['traded_candle'] = last['timestamp']
        save_state(state)

    if state.get('position'):
        try:
            if paper_mode:
                ltp_opt = state['position']['max_price'] + 1
            else:
                quote = client.get_quotes("NFO", state['position']['tradingsymbol'])
                ltp_opt = float(quote['data'][state['position']['tradingsymbol']]['lastprice'])
        except Exception:
            return

        if ltp_opt > state['position']['max_price']:
            state['position']['max_price'] = ltp_opt
            state['position']['sl_price'] = max(state['position']['sl_price'], ltp_opt * 0.95)
            save_state(state)

        if ltp_opt <= state['position']['sl_price']:
            if paper_mode:
                exit_price = ltp_opt
            else:
                order_params = {
                    "variety": "NORMAL",
                    "tradingsymbol": state['position']['tradingsymbol'],
                    "symboltoken": state['position']['option_token'],
                    "transactiontype": "SELL",
                    "exchange": "NFO",
                    "ordertype": "MARKET",
                    "producttype": "INTRADAY",
                    "duration": "DAY",
                    "quantity": lot_size
                }
                try:
                    sell_order = client.placeOrder(order_params)
                    exit_price = sell_order['data']['averageprice']
                except Exception as e:
                    print(f"Exit failed: {e}")
                    return

            pnl = (exit_price - state['position']['entry_price']) * lot_size
            print(f"Trade exited. P&L = {pnl}")
            state['position'] = None
            save_state(state)

async def websocket_handler(state, client, expiry_date, lot_size, paper_mode):
    access_token = client.generateSessionToken()
    async with websockets.connect(WS_BASE_URL) as websocket:
        auth_data = {
            "action": "authenticate",
            "data": {"apiKey": os.environ['API_KEY'], "accessToken": access_token}
        }
        await websocket.send(json.dumps(auth_data))
        tokens = [256265]
        sub_data = {
            "action": "subscribe",
            "instrumentToken": tokens
        }
        await websocket.send(json.dumps(sub_data))

        while True:
            msg = await websocket.recv()
            try:
                message = json.loads(msg)
                if message.get("type") == "m":
                    ticks = message.get("data", [])
                    for tick in ticks:
                        await on_tick(tick, state, client, expiry_date, lot_size, paper_mode)
            except Exception as e:
                print(f"Error in websocket message processing: {e}")

async def main_bot_loop():
    api_key = os.environ.get("API_KEY")
    user_id = os.environ.get("USER_ID")
    password = os.environ.get("PASSWORD")
    expiry_date = os.environ.get("EXPIRY_DATE")
    lot_size = int(os.environ.get("LOT_SIZE", 50))
    paper_mode = os.environ.get("PAPER_MODE", "true").lower() == "true"

    client = SmartConnect(api_key=api_key)
    client.generateSession(user_id, password)

    state = load_state()
    await websocket_handler(state, client, expiry_date, lot_size, paper_mode)
