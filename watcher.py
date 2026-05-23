"""Live Kalshi order book TUI — auto-discovers tennis markets and streams
real-time order book, trades, and price history.

Usage:
    python watcher.py                        # auto-discover all live tennis markets
    python watcher.py TICKER1 TICKER2 ...   # watch specific tickers
    python watcher.py --no TICKER           # start on NO side

Keys: q=quit  y=YES side  n=NO side  arrows=navigate market list
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from typing import Optional

import requests
import websockets
from dotenv import load_dotenv
from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widget import Widget
from textual.widgets import DataTable, Footer, Header

from auth import KalshiAuth
from order_book import OrderBookState

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    filename="watcher.log",
    filemode="a",
    format="%(asctime)s %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)

WS_URL       = "wss://api.elections.kalshi.com/trade-api/ws/v2"
KALSHI_REST  = "https://api.elections.kalshi.com/trade-api/v2"

# Known series with regularly active open markets — checked directly, no crawl needed.
ACTIVE_TENNIS_SERIES = [
    "KXATPMATCH",            # ATP match winner
    "KXWTAMATCH",            # WTA match winner
    "KXATPCHALLENGERMATCH",  # ATP Challenger
    "KXWTACHALLENGERMATCH",  # WTA Challenger
    "KXITTFMENMATCH",        # ITTF table tennis
    "KXTABLETENNIS",         # table tennis
    "KXATPGAME",             # ATP game winner
    "KXWTAGAME",             # WTA game winner
]

# ─── Market discovery ─────────────────────────────────────────────────────────

def _fetch_series_markets(api_key: str, series_ticker: str) -> list[tuple[str, str]]:
    """Synchronous fetch of all open markets for one series (called in thread pool)."""
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    session = requests.Session()
    results: list[tuple[str, str]] = []
    cursor = None
    while True:
        params: dict = {"limit": 100, "status": "open", "series_ticker": series_ticker}
        if cursor:
            params["cursor"] = cursor
        try:
            resp = session.get(f"{KALSHI_REST}/markets", params=params, headers=headers, timeout=10)
            if resp.status_code == 429:
                time.sleep(2)
                continue
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            break
        for m in data.get("markets", []):
            ticker = m.get("ticker", "")
            title  = m.get("title") or ticker
            if ticker:
                results.append((ticker, title))
        cursor = data.get("cursor")
        if not cursor:
            break
    return results


async def discover_tennis_markets(api_key: str) -> list[tuple[str, str]]:
    """Fetch open markets from all known tennis series concurrently."""
    tasks = [
        asyncio.to_thread(_fetch_series_markets, api_key, s)
        for s in ACTIVE_TENNIS_SERIES
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    markets: list[tuple[str, str]] = []
    for r in results:
        if isinstance(r, list):
            markets.extend(r)
    return markets


def _short_title(title: str, ticker: str) -> str:
    """Produce a short display name from the market title."""
    import re

    def _trim(s: str, n: int = 11) -> str:
        s = re.sub(r"\s*[:(].*$", "", s.strip())  # drop ": subtitle" or "(qualifier)"
        return re.sub(r"[?!.\s]+$", "", s)[:n]

    # "Will X win the A vs B: ..." → extract "A vs B"
    m = re.search(r"win the (.+?) vs\.?\s*(.+?)(?:\s*[?:]|$)", title, re.I)
    if m:
        return f"{_trim(m.group(1))} vs {_trim(m.group(2))}"
    # "Will X beat Y ..."
    m = re.search(r"Will (.+?) beat (.+?)(?:\s+in\s+|\?|$)", title, re.I)
    if m:
        return f"{_trim(m.group(1))} vs {_trim(m.group(2))}"
    # "X vs Y"
    m = re.search(r"(.+?)\s+vs\.?\s+(.+)", title, re.I)
    if m:
        return f"{_trim(m.group(1))} vs {_trim(m.group(2))}"
    # Fallback: last two dash-segments of the ticker
    parts = ticker.split("-")
    if len(parts) >= 2:
        return f"{parts[-2][:8]}/{parts[-1][:6]}"
    return ticker[:22]


# ─── Textual message ──────────────────────────────────────────────────────────

class MarketUpdated(Message):
    def __init__(self, ticker: str) -> None:
        self.ticker = ticker
        super().__init__()


# ─── Widgets ──────────────────────────────────────────────────────────────────

_SPARK_BLOCKS = " ▁▂▃▄▅▆▇█"


def _sparkline(prices: list[float], width: int = 60) -> str:
    if not prices:
        return ""
    data = prices[-width:]
    lo, hi = min(data), max(data)
    if hi == lo:
        return "─" * len(data)

    def _b(p: float) -> str:
        return _SPARK_BLOCKS[max(0, min(8, round((p - lo) / (hi - lo) * 8)))]

    return "".join(_b(p) for p in data)


class OrderBookWidget(Widget):
    DEFAULT_CSS = "OrderBookWidget { border: solid $primary; height: 1fr; padding: 0 1; }"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._state: Optional[OrderBookState] = None
        self._side = "yes"

    def update(self, state: OrderBookState, side: str) -> None:
        self._state = state
        self._side = side
        self.refresh()

    def render(self):
        if self._state is None:
            return Text("Select a market from the left panel", style="dim")

        s = self._state
        bid, ask, spread = s.best_bid, s.best_ask, s.spread

        label = self._side.upper()
        if bid is not None and ask is not None:
            self.border_title = (
                f" {s.ticker} [{label}]  "
                f"Bid {bid*100:.0f}¢  Ask {ask*100:.0f}¢  Spread {spread*100:.0f}¢"
            )
        else:
            self.border_title = f" {s.ticker} [{label}]  loading…"

        table = Table(show_header=True, box=None, expand=True, padding=(0, 1))
        table.add_column("BID",  style="bold green", justify="right", min_width=6)
        table.add_column("SIZE", style="green",      justify="right", min_width=9)
        table.add_column("",    min_width=1)
        table.add_column("ASK",  style="bold red",   min_width=6)
        table.add_column("SIZE", style="red",        justify="right", min_width=9)

        bids = s.top_bids(10)
        asks = s.top_asks(10)
        for i in range(max(len(bids), len(asks))):
            bp  = f"{bids[i][0]*100:.0f}¢"   if i < len(bids) else ""
            bs  = f"{bids[i][1]:>9,.0f}"      if i < len(bids) else ""
            ap  = f"{asks[i][0]*100:.0f}¢"   if i < len(asks) else ""
            as_ = f"{asks[i][1]:>9,.0f}"      if i < len(asks) else ""
            table.add_row(bp, bs, "│", ap, as_)

        return table


class TapeWidget(Widget):
    DEFAULT_CSS = "TapeWidget { border: solid $primary; height: 1fr; padding: 0 1; }"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._state: Optional[OrderBookState] = None

    def update(self, state: OrderBookState) -> None:
        self._state = state
        self.refresh()

    def render(self):
        self.border_title = " Trades"
        if self._state is None or not self._state.tape:
            return Text("No trades yet", style="dim")

        table = Table(show_header=True, box=None, expand=True, padding=(0, 1))
        table.add_column("TIME",  min_width=8)
        table.add_column("PRICE", justify="right", min_width=6)
        table.add_column("SIZE",  justify="right", min_width=8)
        table.add_column("SIDE",  min_width=4)

        for trade in list(self._state.tape)[:25]:
            color = "green" if trade.taker_side == "yes" else "red"
            table.add_row(
                trade.time.strftime("%H:%M:%S"),
                f"{trade.yes_price * 100:.0f}¢",
                f"{trade.count:,}",
                Text(trade.taker_side.upper(), style=color),
            )
        return table


class SparklineWidget(Widget):
    DEFAULT_CSS = "SparklineWidget { border: solid $primary; height: 5; padding: 0 1; }"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._state: Optional[OrderBookState] = None

    def update(self, state: OrderBookState) -> None:
        self._state = state
        self.refresh()

    def render(self):
        if self._state is None or not self._state.price_history:
            self.border_title = " Price History"
            return Text("─" * 40 + "  awaiting data…", style="dim")

        history = list(self._state.price_history)
        spark   = _sparkline(history)
        mid     = self._state.mid_price
        delta   = self._state.delta_1m

        title_parts = ["Price History"]
        if mid is not None:
            title_parts.append(f"Mid: {mid*100:.1f}¢")
        if delta is not None:
            sign  = "+" if delta >= 0 else ""
            color = "green" if delta >= 0 else "red"
            title_parts.append(f"[{color}]{sign}{delta*100:.1f}¢[/{color}]")
        self.border_title = "  ".join(title_parts)

        return Text(spark)  # sparkline only in content, stats in border


# ─── App ──────────────────────────────────────────────────────────────────────

class OrderBookApp(App):
    CSS = """
    Screen { layout: horizontal; }
    #left-pane  { width: 42; border: solid $primary; }
    #right-pane { width: 1fr; layout: vertical; }
    OrderBookWidget { height: 2fr; }
    TapeWidget      { height: 1fr; }
    SparklineWidget { height: 7;   }
    """

    BINDINGS = [
        ("q", "quit",     "Quit"),
        ("y", "side_yes", "YES side"),
        ("n", "side_no",  "NO side"),
    ]

    def __init__(
        self,
        tickers: Optional[list[str]] = None,
        side: str = "yes",
    ) -> None:
        super().__init__()
        self._explicit_tickers = tickers
        self._side = side
        self._states: dict[str, OrderBookState] = {}
        self._selected: Optional[str] = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield DataTable(id="left-pane")
            with Vertical(id="right-pane"):
                yield OrderBookWidget(id="orderbook")
                yield TapeWidget(id="tape")
                yield SparklineWidget(id="sparkline")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#left-pane", DataTable)
        table.cursor_type = "row"
        table.add_column("Market", key="name")
        table.add_column("Mid",    key="mid",   width=6)
        table.add_column("Δ",      key="delta", width=6)
        table.add_column("Sprd",   key="spread",width=5)
        self._start_stream()

    # ── Actions ───────────────────────────────────────────────────────────────

    def action_side_yes(self) -> None:
        self._side = "yes"
        self._push_right()

    def action_side_no(self) -> None:
        self._side = "no"
        self._push_right()

    @on(DataTable.RowHighlighted, "#left-pane")
    def on_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key and event.row_key.value:
            self._selected = str(event.row_key.value)
            self._push_right()

    @on(MarketUpdated)
    def on_market_updated(self, event: MarketUpdated) -> None:
        state = self._states.get(event.ticker)
        if not state:
            return
        self._refresh_row(event.ticker, state)
        if event.ticker == self._selected:
            self._push_right()

    # ── UI helpers ────────────────────────────────────────────────────────────

    def _push_right(self) -> None:
        if self._selected is None:
            return
        state = self._states.get(self._selected)
        if state is None:
            return
        self.query_one("#orderbook", OrderBookWidget).update(state, self._side)
        self.query_one("#tape",      TapeWidget).update(state)
        self.query_one("#sparkline", SparklineWidget).update(state)

    def _refresh_row(self, ticker: str, state: OrderBookState) -> None:
        table = self.query_one("#left-pane", DataTable)
        mid    = state.mid_price
        delta  = state.delta_1m
        spread = state.spread
        mid_s    = f"{mid*100:.0f}¢"   if mid    is not None else "—"
        delta_s  = (f"+{delta*100:.1f}" if delta and delta > 0
                    else f"{delta*100:.1f}" if delta else "—")
        spread_s = f"{spread*100:.0f}¢" if spread is not None else "—"
        try:
            table.update_cell(ticker, "name",   state.title[:22], update_width=False)
            table.update_cell(ticker, "mid",    mid_s,             update_width=False)
            table.update_cell(ticker, "delta",  delta_s,           update_width=False)
            table.update_cell(ticker, "spread", spread_s,          update_width=False)
        except Exception:
            pass

    # ── Background stream ─────────────────────────────────────────────────────

    @work(exclusive=True)
    async def _start_stream(self) -> None:
        api_key  = os.environ.get("KALSHI_API_KEY_ID", "")
        key_path = os.environ.get("KALSHI_API_PATH", "")

        if not key_path:
            self.notify("KALSHI_API_PATH not set in .env", severity="error", timeout=10)
            return

        auth = KalshiAuth(api_key=api_key, key_path=key_path)

        if self._explicit_tickers:
            ticker_info = [(t, t) for t in self._explicit_tickers]
        else:
            self.notify("Discovering tennis markets…", timeout=8)
            try:
                ticker_info = await discover_tennis_markets(api_key)
            except Exception as exc:
                self.notify(f"Discovery failed: {exc}", severity="error", timeout=10)
                return
            if not ticker_info:
                self.notify("No open tennis markets found", severity="warning", timeout=10)
                return

        tickers = [t for t, _ in ticker_info]

        for ticker, title in ticker_info:
            state = OrderBookState(ticker)
            state.title = _short_title(title, ticker)
            self._states[ticker] = state

        table = self.query_one("#left-pane", DataTable)
        for ticker, _ in ticker_info:
            state = self._states[ticker]
            table.add_row(state.title[:22], "—", "—", "—", key=ticker)

        if tickers:
            self._selected = tickers[0]

        self.notify(f"Watching {len(tickers)} market{'s' if len(tickers) != 1 else ''}", timeout=3)

        queue: asyncio.Queue = asyncio.Queue()

        async def _dispatch() -> None:
            while True:
                msg    = await queue.get()
                ticker = self._route(msg)
                if ticker:
                    self.post_message(MarketUpdated(ticker))

        backoff = 1.0
        while True:
            try:
                headers = auth.ws_headers()
                async with websockets.connect(
                    WS_URL,
                    additional_headers=headers,
                    ping_interval=20,
                    ping_timeout=10,
                ) as ws:
                    backoff = 1.0
                    await ws.send(json.dumps({
                        "id": 1,
                        "cmd": "subscribe",
                        "params": {
                            "channels": ["orderbook_delta", "trade"],
                            "market_tickers": tickers,
                        },
                    }))
                    dispatch_task = asyncio.create_task(_dispatch())
                    try:
                        while True:
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
                                await queue.put(json.loads(raw))
                            except asyncio.TimeoutError:
                                continue
                    finally:
                        dispatch_task.cancel()
                        try:
                            await dispatch_task
                        except asyncio.CancelledError:
                            pass
            except Exception as exc:
                log.error("WS error: %s", exc)
                self.notify(f"WS dropped — reconnecting in {backoff:.0f}s…", timeout=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def _route(self, msg: dict) -> Optional[str]:
        msg_type = msg.get("type")
        data     = msg.get("msg", {})
        ticker   = data.get("market_ticker")

        if not ticker or ticker not in self._states:
            return None

        state = self._states[ticker]
        if msg_type == "orderbook_snapshot":
            state.apply_snapshot(data)
        elif msg_type == "orderbook_delta":
            state.apply_delta(data)
        elif msg_type == "trade":
            log.info("Trade fields: %s", list(data.keys()))
            state.apply_trade(data)
        else:
            return None

        return ticker


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args    = sys.argv[1:]
    side    = "no" if "--no" in args else "yes"
    tickers = [a for a in args if not a.startswith("--")] or None
    OrderBookApp(tickers=tickers, side=side).run()
