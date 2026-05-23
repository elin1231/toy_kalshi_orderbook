class OrderBook:
    def __init__(self, market_ticker):
        self.market_ticker = market_ticker
        self.yes = {}  # price_cents (int) -> quantity (float)
        self.no = {}

    def _to_cents(self, price_str):
        return round(float(price_str) * 100)

    def apply_snapshot(self, msg):
        # New API format: yes_dollars_fp / no_dollars_fp with [price_str, qty_str] pairs
        self.yes = {self._to_cents(p): float(q) for p, q in msg.get("yes_dollars_fp", [])}
        self.no = {self._to_cents(p): float(q) for p, q in msg.get("no_dollars_fp", [])}
        # Fallback for old integer format from tutorial
        if not self.yes and not self.no:
            self.yes = {int(p): float(q) for p, q in msg.get("yes", [])}
            self.no = {int(p): float(q) for p, q in msg.get("no", [])}

    def apply_delta(self, msg):
        side = self.yes if msg["side"] == "yes" else self.no
        # New API: price_dollars / delta_fp; old API: price / delta
        if "price_dollars" in msg:
            price = self._to_cents(msg["price_dollars"])
            delta = float(msg["delta_fp"])
        else:
            price = int(msg["price"])
            delta = float(msg["delta"])
        new_qty = side.get(price, 0) + delta
        if new_qty <= 0:
            side.pop(price, None)
        else:
            side[price] = new_qty

    def best_bid(self, side="yes"):
        book = self.yes if side == "yes" else self.no
        return max(book.keys()) if book else None

    def best_ask(self, side="yes"):
        # Ask on one side = 100 minus best bid on the other side
        other = self.no if side == "yes" else self.yes
        return (100 - max(other.keys())) if other else None

    def spread(self, side="yes"):
        bid = self.best_bid(side)
        ask = self.best_ask(side)
        if bid is not None and ask is not None:
            return ask - bid
        return None

    def display(self, side="yes"):
        bid = self.best_bid(side)
        ask = self.best_ask(side)
        spread = self.spread(side)
        label = side.upper()

        print(f"\033[2J\033[H", end="")
        print(f"  {self.market_ticker}  [{label}]")
        print(f"  Best Bid: {bid}¢    Best Ask: {ask}¢    Spread: {spread}¢")
        print()

        # Asks — derived from the opposite side's bids
        other = self.no if side == "yes" else self.yes
        other_sorted = sorted(other.items(), reverse=True)[:8]
        print(f"  — {label} Asks —")
        for price, qty in reversed(other_sorted):
            ask_price = 100 - price
            print(f"    {ask_price:3d}¢  {qty:>10,.0f} contracts")

        print(f"  {'—' * 32}")

        # Bids — direct from this side
        own = self.yes if side == "yes" else self.no
        own_sorted = sorted(own.items(), reverse=True)[:8]
        print(f"  — {label} Bids —")
        for price, qty in own_sorted:
            print(f"    {price:3d}¢  {qty:>10,.0f} contracts")

        print()
