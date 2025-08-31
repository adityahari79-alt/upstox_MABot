import streamlit as st
import pandas as pd
import asyncio
import websockets
import json
import os
from datetime import datetime, timedelta
from smartapi import SmartConnect

STATE_FILE = "bot_state_angelone.json"
WS_BASE_URL = "wss://marginsocket.angelbroking.com/smart-stream"

# --- State Persistence ---
def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({
                "candles": st.session_state.candles,
                "position": st.session_state.position,
                "traded_candle": str(st.session_state.traded_candle) if st.session_state.traded_candle else None
            }, f)
    except Exception as e:
        st.error(f"Error saving state: {e}")

def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
            st.session_state.candles = [
                {**c, "timestamp": pd.to_datetime(c["timestamp"])} for c in data.get("candles", [])
            ]
            st.session_state.position = data.get("position", None)
            traded = data.get("traded_candle")
            st.session_state.traded_candle = pd.to_datetime(traded) if traded else None
        else:
            st.session_state.candles = []
            st.session_state.position = None
            st.session_state.traded_candle = None
    except Exception as e:
        st.error(f"Error loading state: {e}")

# --- Utility Functions ---
def round_strike(price, interval=50):
    return int(price // interval * interval)

def get_option_instrument_token(strike, expiry_date, client: SmartConnect):
    try:
        instruments = client.searchInstruments(exchange="NFO", symbol="NIFTY")
    except Exception:
        return None, None

    for inst in instruments:
        if 'expiry' not in inst or 'strikeprice' not in inst or 'optiontype' not in inst:
            continue
        inst_expiry_str = inst['expiry'].strftime("%Y-%m-%d")
        if (inst_expiry_str == expiry_date and
            inst['strikeprice'] == strike and
            inst['optiontype'].upper() == 'CE'):
            return inst['symboltoken'], inst['tradingsymbol']
    return None, None

def update_candles(ts, price, minutes=5):
    candles = st.session_state.candles
    start = ts - timedelta(minutes=ts.minute % minutes, seconds=ts.second, microseconds=ts.microsecond)
    if not candles or candles[-1]['timestamp'] != start:
        candles.append({"timestamp": start, "open": price, "high": price, "low": price, "close": price})
    else:
        c = candles[-1]
        c['high'] = max(c['high'], price)
        c['low'] = min(c['low'], price)
        c['close'] = price
    st.session_state.candles = candles

async def on_tick(tick):
    try:
        ts = datetime.fromtimestamp(tick['timestamp'] / 1000)
        ltp = float(tick['lastprice'])
    except Exception:
        return

    update_candles(ts, ltp)
    save_state()

    df = pd.DataFrame(st.session_state.candles)
    if len(df) < 21:
        return

    df['ma10'] = df['close'].rolling(10).mean()
    df['ma21'] = df['close'].rolling(21).mean()
    last = df.iloc[-2]

    if (last['ma10'] >= last['ma21'] and
        st.session_state.traded_candle != last['timestamp'] and
        not st.session_state.position):

        strike = round_strike(last['close']) - 200
        opt_token, opt_symbol = get_option_instrument_token(strike, st.session_state.expiry_date, st.session_state.client)
        if not opt_token:
            st.session_state.status_box.warning("Option instrument not found.")
            return

        if st.session_state.paper_mode:
            entry_price = last['close']
            st.session_state.trade_log.write(f"[PAPER] Bought {strike} CE @ {entry_price}")
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
                "quantity": st.session_state.lot_size
            }
            try:
                order_response = st.session_state.client.placeOrder(order_params)
                entry_price = order_response['data']['averageprice']
                st.session_state.trade_log.write(f"Bought {strike} CE @ {entry_price}")
            except Exception as e:
                st.session_state.status_box.error(f"Buy failed: {e}")
                return

        st.session_state.position = {
            'option_token': opt_token,
            'tradingsymbol': opt_symbol,
            'entry_price': entry_price,
            'sl_price': entry_price * 0.95,
            'max_price': entry_price
        }
        st.session_state.traded_candle = last['timestamp']
        save_state()

    # Manage open position
    if st.session_state.position:
        try:
            if st.session_state.paper_mode:
                ltp_opt = st.session_state.position['max_price'] + 1
            else:
                quote = st.session_state.client.get_quotes("NFO", st.session_state.position['tradingsymbol'])
                ltp_opt = float(quote['data'][st.session_state.position['tradingsymbol']]['lastprice'])
        except Exception:
            return

        if ltp_opt > st.session_state.position['max_price']:
            st.session_state.position['max_price'] = ltp_opt
            st.session_state.position['sl_price'] = max(st.session_state.position['sl_price'], ltp_opt * 0.95)
            save_state()

        if ltp_opt <= st.session_state.position['sl_price']:
            if st.session_state.paper_mode:
                exit_price = ltp_opt
            else:
                try:
                    order_params = {
                        "variety": "NORMAL",
                        "tradingsymbol": st.session_state.position['tradingsymbol'],
                        "symboltoken": st.session_state.position['option_token'],
                        "transactiontype": "SELL",
                        "exchange": "NFO",
                        "ordertype": "MARKET",
                        "producttype": "INTRADAY",
                        "duration": "DAY",
                        "quantity": st.session_state.lot_size
                    }
                    sell_order = st.session_state.client.placeOrder(order_params)
                    exit_price = sell_order['data']['averageprice']
                except Exception as e:
                    st.session_state.status_box.error(f"Exit failed: {e}")
                    return

            pnl = (exit_price - st.session_state.position['entry_price']) * st.session_state.lot_size
            st.session_state.pnl_box.success(f"Trade exited. P&L = {pnl}")
            st.session_state.position = None
            save_state()

async def websocket_handler():
    # WebSocket connection with Angel One's margin socket server
    api_key = st.session_state.api_key
    client = st.session_state.client
    access_token = client.generateSessionToken()  # Or use stored access token

    async with websockets.connect(WS_BASE_URL) as websocket:
        # Authenticate connection
        auth_data = {
            "action": "authenticate",
            "data": {"apiKey": api_key, "accessToken": access_token}
        }
        await websocket.send(json.dumps(auth_data))

        # Subscribe to tokens (replace with your tokens)
        # You must get token list from instruments used (NIFTY, options, etc.)
        tokens_to_subscribe = [256265]  # example NIFTY token
        sub_data = {
            "action": "subscribe",
            "instrumentToken": tokens_to_subscribe
        }
        await websocket.send(json.dumps(sub_data))

        while True:
            msg = await websocket.recv()
            try:
                message = json.loads(msg)
                if message.get("type") == "m":
                    ticks = message.get("data", [])
                    for tick in ticks:
                        await on_tick(tick)
            except Exception as ex:
                print("Error processing WS message:", ex)

# --- Streamlit Bot Page ---
def trading_bot_page():
    st.title("Angel One Nifty50 MA Bot (Full Async WebSocket Integration)")

    st.session_state.api_key = st.sidebar.text_input("API Key")
    user_id = st.sidebar.text_input("User ID")
    password = st.sidebar.text_input("Password", type="password")

    st.session_state.expiry_date = st.sidebar.text_input("Option Expiry Date (YYYY-MM-DD)")
    st.session_state.lot_size = st.sidebar.number_input("Lot Size", value=50, min_value=1)
    st.session_state.paper_mode = st.sidebar.checkbox("Paper Mode (No real orders)", True)

    if "candles" not in st.session_state:
        load_state()

    st.session_state.status_box = st.empty()
    st.session_state.trade_log = st.empty()
    st.session_state.pnl_box = st.empty()

    start_bot = st.sidebar.button("Start Bot")

    if start_bot:
        if not (st.session_state.api_key and user_id and password and st.session_state.expiry_date):
            st.error("Fill all API & config fields")
            return

        # Create SmartAPI session
        client = create_smartapi_session(st.session_state.api_key, user_id, password)
        st.session_state.client = client

        # Run websocket listener asynchronously
        loop = asyncio.new_event_loop()
        st.session_state.loop = loop
        asyncio.set_event_loop(loop)
        st.info("Bot started, connecting to WebSocket...")

        try:
            loop.run_until_complete(websocket_handler())
        except Exception as e:
            st.error(f"WebSocket error: {e}")

# --- Main Navigation ---
def main():
    st.sidebar.title("Navigation")
    page = st.sidebar.radio("Go to", ["Trading Bot"])
    if page == "Trading Bot":
        trading_bot_page()

if __name__ == "__main__":
    main()
