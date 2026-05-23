import asyncio
import json
import os
import sys

import websockets
from dotenv import load_dotenv

from auth import KalshiAuth
from order_book import OrderBook

load_dotenv()

WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"

auth = KalshiAuth(
    api_key=os.environ["KALSHI_API_KEY_ID"],
    key_path=os.environ["KALSHI_API_PATH"],
)


async def watch_orderbook(ticker, side="yes", debug=False):
    headers = auth.ws_headers()
    book = OrderBook(ticker)

    print(f"Connecting to {WS_URL}...")
    async with websockets.connect(WS_URL, additional_headers=headers) as ws:
        await ws.send(json.dumps({
            "id": 1,
            "cmd": "subscribe",
            "params": {
                "channels": ["orderbook_delta"],
                "market_tickers": [ticker],
            },
        }))
        print(f"Subscribed to orderbook_delta for {ticker}")

        while True:
            raw = await ws.recv()
            data = json.loads(raw)

            if debug:
                print(json.dumps(data, indent=2))

            msg_type = data.get("type")

            if msg_type == "subscribed":
                print(f"Subscribed to {data['msg']['channel']}")

            elif msg_type == "orderbook_snapshot":
                book.apply_snapshot(data["msg"])
                if not debug:
                    book.display(side)

            elif msg_type == "orderbook_delta":
                book.apply_delta(data["msg"])
                if not debug:
                    book.display(side)

            elif msg_type == "error":
                print(f"Error: {data.get('msg')}")
                break


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python watcher.py <MARKET_TICKER> [--no] [--debug]")
        sys.exit(1)

    ticker = sys.argv[1]
    side = "no" if "--no" in sys.argv else "yes"
    debug = "--debug" in sys.argv

    asyncio.run(watch_orderbook(ticker, side=side, debug=debug))
