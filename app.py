import asyncio
from fastapi import FastAPI
from threading import Thread
import bot

app = FastAPI()

bot_loop = None
bot_thread = None

def run_bot():
    global bot_loop
    bot_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(bot_loop)
    bot_loop.run_until_complete(bot.main_bot_loop())

@app.get("/")
def read_root():
    return {"message": "Angel One Trading Bot Server running."}

@app.post("/start")
def start_bot():
    global bot_thread
    if bot_thread and bot_thread.is_alive():
        return {"status": "Bot already running"}
    bot_thread = Thread(target=run_bot, daemon=True)
    bot_thread.start()
    return {"status": "Bot started"}

@app.post("/stop")
def stop_bot():
    global bot_loop
    if bot_loop:
        bot_loop.stop()
        return {"status": "Bot stopping"}
    return {"status": "Bot not running"}
