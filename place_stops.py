"""Place 5% trailing stop sells on all open positions that don't already have one."""

from trade import client, trailing_stop_sell
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus


def place_missing_stops(trail_percent=5.0):
    # Get all positions
    positions = client.get_all_positions()
    if not positions:
        print("No open positions.")
        return

    # Get existing trailing stop orders
    open_orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
    stops_placed = set()
    for o in open_orders:
        if o.type.value == "trailing_stop":
            stops_placed.add(o.symbol)

    print(f"Positions: {len(positions)}")
    print(f"Existing trailing stops: {stops_placed or 'none'}\n")

    placed = 0
    for p in positions:
        if p.symbol in stops_placed:
            print(f"  {p.symbol:<8} already has trailing stop, skipping")
            continue
        try:
            print(f"  {p.symbol:<8} placing {trail_percent}% trailing stop on {p.qty} share(s)...")
            trailing_stop_sell(p.symbol, float(p.qty), trail_percent=trail_percent)
            placed += 1
        except Exception as e:
            print(f"  {p.symbol:<8} ERROR: {e}")

    print(f"\nDone: {placed} trailing stops placed.")


if __name__ == "__main__":
    place_missing_stops()
