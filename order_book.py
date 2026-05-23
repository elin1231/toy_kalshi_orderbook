"""In-memory order book state for a single Kalshi market.

Kalshi's YES/NO model:
  - yes dict: prices where people bid to buy YES (bid side)
  - no dict:  prices where people bid to buy NO
  - YES ask price = 1.0 - best NO bid price  (buying NO at X = selling YES at 1-X)
  - Spread = best_ask - best_bid
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class Trade:
    time: datetime
    yes_price: float   # 0.0–1.0
    count: int
    taker_side: str    # "yes" or "no"


class OrderBookState:
    def __init__(self, ticker: str) -> None:
        self.ticker = ticker
        self.yes: dict[float, float] = {}   # price -> qty (YES bids)
        self.no: dict[float, float] = {}    # price -> qty (NO bids)
        self.tape: deque[Trade] = deque(maxlen=50)
        self.price_history: deque[float] = deque(maxlen=300)
        self.title: str = ticker            # display name (set by caller if available)

    def apply_snapshot(self, msg: dict) -> None:
        self.yes = {round(float(p), 4): float(q) for p, q in msg.get("yes_dollars_fp", [])}
        self.no  = {round(float(p), 4): float(q) for p, q in msg.get("no_dollars_fp",  [])}
        self._record_mid()

    def apply_delta(self, msg: dict) -> None:
        side  = self.yes if msg["side"] == "yes" else self.no
        price = round(float(msg["price_dollars"]), 4)
        delta = float(msg["delta_fp"])
        new_qty = side.get(price, 0.0) + delta
        if new_qty <= 0:
            side.pop(price, None)
        else:
            side[price] = new_qty
        self._record_mid()

    def apply_trade(self, msg: dict) -> None:
        # Kalshi v2 WS trade fields (confirmed from live messages):
        #   yes_price_dollars: str e.g. "0.8500"
        #   count_fp:          str e.g. "10.00"
        # Older fallbacks kept for any legacy format.
        for field in ("yes_price_dollars", "yes_price_fp", "yes_price", "price_dollars", "price"):
            raw = msg.get(field)
            if raw is not None:
                break
        else:
            raw = 0
        yes_price = float(raw)
        if yes_price > 1.0:   # legacy integer-cents format
            yes_price /= 100.0
        count = max(1, round(float(msg.get("count_fp") or msg.get("count") or 1)))
        taker_side = msg.get("taker_side", "yes")
        ts_str     = msg.get("ts")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")) if ts_str else datetime.now(timezone.utc)
        except (ValueError, AttributeError):
            ts = datetime.now(timezone.utc)
        self.tape.appendleft(Trade(time=ts, yes_price=yes_price, count=count, taker_side=taker_side))

    @property
    def best_bid(self) -> Optional[float]:
        return max(self.yes.keys()) if self.yes else None

    @property
    def best_ask(self) -> Optional[float]:
        return (1.0 - max(self.no.keys())) if self.no else None

    @property
    def spread(self) -> Optional[float]:
        bid, ask = self.best_bid, self.best_ask
        return (ask - bid) if bid is not None and ask is not None else None

    @property
    def mid_price(self) -> Optional[float]:
        bid, ask = self.best_bid, self.best_ask
        if bid is not None and ask is not None:
            return (bid + ask) / 2.0
        return bid if bid is not None else ask

    @property
    def delta_1m(self) -> Optional[float]:
        if len(self.price_history) < 2:
            return None
        return self.price_history[-1] - self.price_history[0]

    def top_bids(self, n: int = 8) -> list[tuple[float, float]]:
        """Top N YES bids, highest first."""
        return sorted(self.yes.items(), reverse=True)[:n]

    def top_asks(self, n: int = 8) -> list[tuple[float, float]]:
        """Top N YES asks, lowest first. Derived by inverting NO bids."""
        inverted = [(1.0 - p, q) for p, q in self.no.items()]
        return sorted(inverted)[:n]

    def _record_mid(self) -> None:
        mid = self.mid_price
        if mid is not None:
            self.price_history.append(mid)
