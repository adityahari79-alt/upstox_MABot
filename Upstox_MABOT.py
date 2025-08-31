import streamlit as st
import pandas as pd
import asyncio
from datetime import datetime, timedelta
from upstox_api.api import Upstox, MarketFeedType, OrderType, TransactionType, ProductType
import json
import os
import requests
from urllib.parse import urlencode

STATE_FILE = "bot_state_upstox.json"

# --- Shared functions ---

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

def round_strike(price, interval=50):
    return int(price // interval * interval)

def get_option_instrument_token(strike, expiry_date, client: Upstox):
    try:
        instruments = client.get_instruments('NFO')
    except Exception:
        return None
    for inst in instruments:
        if not inst.get('expiry') or not inst.get('strike_price') or not inst.get('option_type'):
            continue
        inst_expiry_str = inst['expiry'].strftime("%Y-%m-%d")
        if inst_expiry_str == expiry_date and inst['strike_price'] == strike and inst['option_type'] == 'CE':
            return inst['instrument_token']
    return None

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
        ltp = float(tick['last_price'])
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
        opt_id = get_option_instrument_token(strike, st.session_state.expiry_date, st.session_state.u)
        if not opt_id:
            st.session_state.status_box.warning("Option instrument not found.")
            return

        if st.session_state.paper_mode:
            entry_price = last['close']
            st.session_state.trade_log.write(f"[PAPER] Bought {strike} CE @ {entry_price}")
        else:
            try:
                order = st.session_state.u.place_order(
                    opt_id,
                    quantity=st.session_state.lot_size,
                    order_type=OrderType.Market,
                    product_type=ProductType.Intraday,
                    transaction_type=TransactionType.Buy,
                    price=0
                )
                entry_price = order['price'] if 'price' in order else last['close']
                st.session_state.trade_log.write(f"Bought {strike} CE @ {entry_price}")
            except Exception as e:
                st.session_state.status_box.error(f"Buy failed: {e}")
                return

        st.session_state.position = {
            'option_id': opt_id,
            'entry_price': entry_price,
            'sl_price': entry_price * 0.95,
            'max_price': entry_price
        }
        st.session_state.traded_candle = last['timestamp']
        save_state()

    if st.session_state.position:
        try:
            if st.session_state.paper_mode:
                ltp_opt = st.session_state.position['max_price'] + 1
            else:
                quote_response = st.session_state.u.get_live_feed([st.session_state.position['option_id']])
                ltp_opt = float(quote_response[0]['last_price'])
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
                    sell_order = st.session_state.u.place_order(
                        st.session_state.position['option_id'],
                        quantity=st.session_state.lot_size,
                        order_type=OrderType.Market,
                        product_type=ProductType.Intraday,
                        transaction_type=TransactionType.Sell,
                        price=0
                    )
                    exit_price = sell_order['price'] if 'price' in sell_order else ltp_opt
                except Exception as e:
                    st.session_state.status_box.error(f"Exit failed: {e}")
                    return

            pnl = (exit_price - st.session_state.position['entry_price']) * st.session_state.lot_size
            st.session_state.pnl_box.success(f"Trade exited. P&L = {pnl}")
            st.session_state.position = None
            save_state()

def handle_ws_message(message):
    import json
    tick_data = json.loads(message)
    asyncio.run_coroutine_threadsafe(on_tick(tick_data), st.session_state.loop)

# --- OAuth Page ---

def oauth_page():
    st.title("Upstox OAuth Token Generator")

    API_BASE_AUTH_URL = "https://upstox.com/mapi/oauth2/authorize"
    API_BASE_TOKEN_URL = "https://upstox.com/mapi/oauth2/token"

    def generate_auth_url(api_key, redirect_uri, state=""):
        params = {
            "apiKey": api_key,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "state": state
        }
        from urllib.parse import urlencode
        return f"{API_BASE_AUTH_URL}?{urlencode(params)}"

    def exchange_code_for_token(api_key, api_secret, redirect_uri, auth_code):
        data = {
            "apiKey": api_key,
            "apiSecret": api_secret,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code": auth_code
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        response = requests.post(API_BASE_TOKEN_URL, data=data, headers=headers)
        return response

    api_key = st.text_input("API Key")
    api_secret = st.text_input("API Secret", type="password")
    redirect_uri = st.text_input("Redirect URI")
    state = st.text_input("State (optional)")

    if st.button("Generate Authorization URL"):
        if not (api_key and redirect_uri):
            st.error("Please enter API Key and Redirect URI")
        else:
            url = generate_auth_url(api_key, redirect_uri, state)
            st.markdown(f"### Authorization URL")
            st.write(url)
            st.markdown("Open this URL in your browser, login, allow access, and copy the `code` parameter from the redirected URL.")

    auth_code = st.text_input("Authorization Code (from redirect URL)")

    if st.button("Get Access Token"):
        if not (api_key and api_secret and redirect_uri and auth_code):
            st.error("Fill all fields before requesting access token")
        else:
            resp = exchange_code_for_token(api_key, api_secret, redirect_uri, auth_code)
            if resp.status_code == 200:
                token_data = resp.json()
                st.success("Access token obtained successfully!")
                st.write("Access Token:", token_data.get("access_token"))
                st.write("Refresh Token:", token_data.get("refresh_token"))
                st.write("Expires In (seconds):", token_data.get("expires_in"))
            else:
                st.error(f"Failed to obtain token: {resp.text}")

# --- Trading Bot Page ---

def trading_bot_page():
    st.title("Upstox Nifty50 MA Bot")

    API_KEY = st.sidebar.text_input("API Key")
    ACCESS_TOKEN = st.sidebar.text_input("Access Token")
    nifty_token = st.sidebar.text_input("Nifty 50 Instrument Token (e.g., 256265)")
    expiry_date = st.sidebar.text_input("Option Expiry Date (YYYY-MM-DD)")
    lot_size = st.sidebar.number_input("Lot Size", value=50, min_value=1)
    paper_mode = st.sidebar.checkbox("Paper Mode (No real orders)", True)
    start_bot = st.sidebar.button("Start Bot")

    if "candles" not in st.session_state:
        load_state()

    st.session_state.lot_size = lot_size
    st.session_state.paper_mode = paper_mode
    st.session_state.expiry_date = expiry_date

    st.session_state.status_box = st.empty()
    st.session_state.trade_log = st.empty()
    st.session_state.pnl_box = st.empty()

    if start_bot:
        if not (API_KEY and ACCESS_TOKEN and nifty_token and expiry_date):
            st.error("Fill all API & config fields")
            return

        u = Upstox(API_KEY, ACCESS_TOKEN)
        st.session_state.u = u

        loop = asyncio.new_event_loop()
        st.session_state.loop = loop
        asyncio.set_event_loop(loop)

        u.start_websocket(handle_ws_message, MarketFeedType.Full)
        u.subscribe([int(nifty_token)])

        loop.run_forever()

# --- Main App Navigation ---

PAGES = {
    "OAuth Token Generator": oauth_page,
    "Trading Bot": trading_bot_page,
}

def main():
    st.sidebar.title("Navigation")
    page = st.sidebar.radio("Go to", list(PAGES.keys()))
    PAGES[page]()

if __name__ == "__main__":
    main()
