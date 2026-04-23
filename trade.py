import os
from typing import Any
import requests
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, TrailingStopOrderRequest, LimitOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus

load_dotenv()


def env_flag(name: str, default: bool = False) -> bool:
    """Parse a boolean environment variable."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def get_alpaca_credentials(use_paper: bool) -> tuple[str, str]:
    """Return the correct Alpaca keypair. Live mode requires PROD_* keys explicitly; no fallback."""
    if use_paper:
        key = os.getenv("ALPACA_API_KEY")
        secret = os.getenv("ALPACA_SECRET_KEY")
        mode = "paper"
        expected = "ALPACA_API_KEY / ALPACA_SECRET_KEY"
    else:
        key = os.getenv("PROD_ALPACA_API_KEY")
        secret = os.getenv("PROD_ALPACA_SECRET_KEY")
        mode = "live"
        expected = "PROD_ALPACA_API_KEY / PROD_ALPACA_SECRET_KEY"

    if not key or not secret:
        raise RuntimeError(
            f"Missing Alpaca {mode} API credentials. Set {expected} in .env "
            f"(ALPACA_PAPER={'true' if use_paper else 'false'})."
        )

    return key, secret


# Paper trading is the default — flip ALPACA_PAPER=false explicitly to trade live.
IS_PAPER_TRADING = env_flag("ALPACA_PAPER", default=True)
ALPACA_API_KEY, ALPACA_SECRET_KEY = get_alpaca_credentials(IS_PAPER_TRADING)


client = TradingClient(
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY,
    paper=IS_PAPER_TRADING,
)
print(f"[trade] Mode: {'PAPER' if IS_PAPER_TRADING else 'LIVE'}")


def has_open_position(symbol: str) -> bool:
    """Return True when the account already holds the symbol."""
    symbol = symbol.upper()
    positions = client.get_all_positions()
    return any(p.symbol.upper() == symbol for p in positions)


def is_market_open() -> bool:
    """Return True if US equities market is open right now."""
    return bool(getattr(client.get_clock(), "is_open", False))


def is_tradable(symbol: str) -> bool:
    """Return True if Alpaca lists the symbol as an active, tradable asset."""
    try:
        asset = client.get_asset(symbol.upper())
    except Exception:
        return False
    status = getattr(asset, "status", None)
    status_str = getattr(status, "value", str(status or ""))
    return bool(getattr(asset, "tradable", False)) and status_str.lower() == "active"


def get_latest_ask(symbol: str) -> float:
    """Fetch the latest ask price from Alpaca market data."""
    url = f"https://data.alpaca.markets/v2/stocks/{symbol.upper()}/quotes/latest"
    headers = {
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    return float(resp.json()["quote"]["ap"])


PDT_EQUITY_MIN = 25_000  # FINRA PDT threshold
PDT_DAYTRADE_LIMIT = 3   # block new opening buys when count reaches this (next would be the 4th)


def pdt_block_reason(account, max_daytrades: int = PDT_DAYTRADE_LIMIT) -> str | None:
    """Return a block-reason string if opening a new position is unsafe for PDT, else None.

    PDT rule: 4+ day trades within any 5 business days in a margin account
    triggers the "pattern day trader" designation and, if equity < $25k,
    restricts further day trading for 90 days. Because we always attach a
    trailing stop sell on the same day as the buy, any new buy carries the
    risk of becoming a day trade. We therefore refuse to open when:

      - The account is already flagged PDT and equity is under $25k (hard
        restriction is already in effect).
      - The account is not flagged PDT but has already accumulated
        ``max_daytrades`` day trades in the trailing 5 business days; one
        more would trigger the designation.

    Accounts already flagged PDT with equity >= $25k are allowed unlimited
    day trades, so the count check is skipped for them.
    """
    if getattr(account, "account_blocked", False):
        return "Account is blocked by Alpaca."
    if getattr(account, "trading_blocked", False):
        return "Trading is blocked on this account."
    if getattr(account, "trade_suspended_by_user", False):
        return "Trading is suspended by user (account setting)."

    equity = float(getattr(account, "equity", 0) or 0)
    daytrade_count = int(getattr(account, "daytrade_count", 0) or 0)
    is_pdt = bool(getattr(account, "pattern_day_trader", False))

    if is_pdt and equity < PDT_EQUITY_MIN:
        return (
            f"Account is flagged as Pattern Day Trader with equity ${equity:,.2f} "
            f"< ${PDT_EQUITY_MIN:,} — opening trades are restricted for 90 days."
        )
    if not is_pdt and daytrade_count >= max_daytrades:
        return (
            f"{daytrade_count} day trade(s) in the last 5 business days "
            f"(threshold {max_daytrades}). A new buy with a same-day trailing "
            f"stop could trigger the 4th day trade and flag the account as PDT."
        )
    return None


def get_account_info():
    account = client.get_account()
    print(f"Trading Mode:   {'Paper' if IS_PAPER_TRADING else 'Live'}")
    print(f"Account Status: {account.status}")
    print(f"Buying Power:   ${float(account.buying_power):,.2f}")
    print(f"Portfolio Value: ${float(account.portfolio_value):,.2f}")
    return account


def submit_order(symbol: str, qty: float, side: OrderSide):
    order_data = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=side,
        time_in_force=TimeInForce.DAY,
    )
    order = client.submit_order(order_data)
    print(f"\nOrder submitted!")
    print(f"  Symbol: {order.symbol}")
    print(f"  Qty:    {order.qty}")
    print(f"  Side:   {order.side}")
    print(f"  Type:   {order.type}")
    print(f"  Status: {order.status}")
    print(f"  ID:     {order.id}")
    return order


def wait_for_order_fill(order_id: str, poll_interval: float = 0.5, max_checks: int = 120):
    """Poll an order until it fills or the retry limit is reached."""
    import time

    order_status = client.get_order_by_id(str(order_id))
    if order_status.status.value == "filled":
        return order_status

    print(".", end="", flush=True)
    for _ in range(max_checks - 1):
        time.sleep(poll_interval)
        order_status = client.get_order_by_id(str(order_id))
        if order_status.status.value == "filled":
            return order_status
        print(".", end="", flush=True)

    return order_status


def buy_with_trailing_stop(symbol: str, qty: float, trail_percent: float = None, trail_price: float = None):
    """Buy stock at market, then place a trailing stop sell order once filled.

    Alpaca trailing stops do not support fractional shares, so qty is forced
    to a whole number. Pass qty >= 1 or this raises.
    """
    symbol = symbol.upper()

    if not trail_percent and not trail_price:
        raise ValueError("Provide either trail_percent or trail_price")

    qty = int(qty)
    if qty < 1:
        raise ValueError("qty must be >= 1 (trailing stops require whole shares)")

    # PDT pre-check — refuse if a new opening buy would risk the 4th day trade.
    account: Any = client.get_account()
    reason = pdt_block_reason(account)
    if reason:
        raise RuntimeError(f"PDT guard blocked buy: {reason}")

    if has_open_position(symbol):
        print(f"=== Skipping {symbol} ===")
        print("  You already have an open position, so no new buy order was submitted.")
        return None, None

    # Step 1: Buy at market
    print(f"=== Buying {qty} share(s) of {symbol} ===")
    buy_order = submit_order(symbol, qty, OrderSide.BUY)

    # Step 2: Wait for buy to fill
    print("\nWaiting for buy order to fill...", end="", flush=True)
    order_status = wait_for_order_fill(str(buy_order.id))
    if order_status.status.value == "filled":
        print(f" filled at ${float(order_status.filled_avg_price):,.2f}")
    else:
        print(f"\nBuy order not yet filled (status: {order_status.status})")
        print("The trailing stop will need to be placed after the buy fills.")
        print(f"Run: trailing_stop_sell('{symbol}', {qty}, trail_percent={trail_percent})")
        return buy_order, None

    # Step 3: Place trailing stop sell order
    stop_order = trailing_stop_sell(symbol, qty, trail_percent=trail_percent, trail_price=trail_price)
    return buy_order, stop_order


def trailing_stop_sell(symbol: str, qty: float, trail_percent: float = None, trail_price: float = None):
    """Place a trailing stop sell on an existing position."""
    if not trail_percent and not trail_price:
        raise ValueError("Provide either trail_percent or trail_price")

    trail_params = {}
    if trail_percent:
        trail_params["trail_percent"] = trail_percent
    else:
        trail_params["trail_price"] = trail_price

    order_data = TrailingStopOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.GTC,
        **trail_params,
    )
    order = client.submit_order(order_data)

    trail_desc = f"{trail_percent}%" if trail_percent else f"${trail_price}"
    print(f"\nTrailing stop sell order placed!")
    print(f"  Symbol:     {order.symbol}")
    print(f"  Qty:        {order.qty}")
    print(f"  Trail:      {trail_desc}")
    print(f"  Status:     {order.status}")
    print(f"  ID:         {order.id}")
    return order


def ladder_buy(symbol: str, total_qty: int, steps: int, step_percent: float, trail_percent: float = None):
    """Place ladder limit buy orders at descending price levels.

    Args:
        symbol: Stock ticker
        total_qty: Total shares to buy across all steps
        steps: Number of rungs in the ladder
        step_percent: Percent drop between each rung
        trail_percent: If set, attach a trailing stop sell to the first (market) rung
    """
    symbol = symbol.upper()

    if has_open_position(symbol):
        print(f"=== Skipping ladder buy for {symbol} ===")
        print("  You already have an open position, so no ladder orders were submitted.")
        return []

    current_price = get_latest_ask(symbol)

    qty_per_step = total_qty // steps
    remainder = total_qty % steps

    print(f"=== Ladder Buy: {symbol} ===")
    print(f"  Current ask:  ${current_price:,.2f}")
    print(f"  Total qty:    {total_qty}")
    print(f"  Steps:        {steps}")
    print(f"  Step drop:    {step_percent}%")
    print(f"  Qty per step: {qty_per_step} (first rung gets +{remainder} remainder)")
    print()

    orders = []
    for i in range(steps):
        qty = qty_per_step + (remainder if i == 0 else 0)
        if qty == 0:
            continue

        if i == 0:
            # First rung: market order (buy now)
            print(f"  Rung {i+1}: {qty} shares @ MARKET")
            order_data = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            )
        else:
            # Subsequent rungs: limit orders at descending prices
            limit_price = round(current_price * (1 - step_percent / 100 * i), 2)
            print(f"  Rung {i+1}: {qty} shares @ ${limit_price:,.2f} ({step_percent * i:.1f}% below current)")
            order_data = LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.GTC,
                limit_price=limit_price,
            )

        order = client.submit_order(order_data)
        orders.append(order)
        print(f"         -> {order.status} (ID: {order.id})")

    # Optionally add trailing stop on the first rung
    if trail_percent and qty_per_step + remainder > 0:
        print(f"\n  Trailing stop: {trail_percent}% on first rung ({qty_per_step + remainder} shares)")
        # Wait for market order to fill before placing trailing stop
        first_order = orders[0]
        print("  Waiting for market order to fill...", end="", flush=True)
        status = wait_for_order_fill(str(first_order.id))
        if status.status.value == "filled":
            print(f" filled at ${float(status.filled_avg_price):,.2f}")
        else:
            print(f" not yet filled ({status.status}). Place trailing stop manually later.")
            return orders

        stop = trailing_stop_sell(symbol, qty_per_step + remainder, trail_percent=trail_percent)
        orders.append(stop)

    print(f"\n  {len(orders)} orders placed.")
    return orders


def mirror_congress_buys(
    qty_per_trade: int = 1,
    trail_percent: float = 5.0,
    days: int = 15,
    max_spend: float | None = None,
    max_daytrades: int = PDT_DAYTRADE_LIMIT,
):
    """Fetch recent congressional buys and mirror them with trailing stops.

    Guards for live/limited-fund trading:
      - Refuses to run when the market is closed (DAY market orders would queue
        and fill at the next open with unpredictable prices).
      - Refuses to run when a new opening buy would risk triggering PDT
        (see ``pdt_block_reason``).
      - Caps total dollars committed to min(buying_power, max_spend).
      - Skips tickers Alpaca does not list as active and tradable.
      - Skips any ticker whose estimated cost (ask * qty * 1.01 slippage
        buffer) would exceed the remaining budget.

    Args:
        qty_per_trade: Whole shares per ticker. Forced to int since trailing
            stops do not support fractional shares.
        trail_percent: Trailing stop percent below peak price.
        days: Lookback window on Capitol Trades.
        max_spend: Optional dollar cap on total commitment. Defaults to full
            buying power.
        max_daytrades: Block opening buys once the rolling 5-day day-trade
            count reaches this. Default 3 leaves no headroom for a 4th.
    """
    from capitol import get_congress_buys

    qty_per_trade = int(qty_per_trade)
    if qty_per_trade < 1:
        raise ValueError("qty_per_trade must be >= 1")

    if not is_market_open():
        print("Market is closed — aborting. Market buys would queue for next open at unpredictable prices.")
        return []

    buys = get_congress_buys(days)
    if not buys:
        print("No congressional buys found.")
        return []

    # Deduplicate by ticker (only buy each stock once)
    seen = set()
    unique_buys = []
    for t in buys:
        if t["ticker"] not in seen:
            seen.add(t["ticker"])
            unique_buys.append(t)

    account: Any = client.get_account()
    reason = pdt_block_reason(account, max_daytrades=max_daytrades)
    if reason:
        print(f"PDT guard: {reason}")
        print("Aborting mirror run — no orders placed.")
        return []

    buying_power = float(account.buying_power)
    budget = min(buying_power, float(max_spend)) if max_spend is not None else buying_power

    print(f"=== Mirroring {len(unique_buys)} Congressional Buys ===")
    print(f"  Buying power:  ${buying_power:,.2f}")
    print(f"  Budget cap:    ${budget:,.2f}")
    print(f"  Qty per trade: {qty_per_trade}")
    print(f"  Trail:         {trail_percent}%\n")

    # Fetch held positions once so we don't re-query per ticker.
    positions: Any = client.get_all_positions()
    held = {p.symbol.upper() for p in positions}

    results = []
    placed = 0
    for t in unique_buys:
        ticker = t["ticker"].upper()
        print(f"--- {ticker}  ({t['politician']}, {t['date']}) ---")

        if ticker in held:
            print(f"  Already holding {ticker}, skipping.\n")
            results.append((ticker, None))
            continue

        if not is_tradable(ticker):
            print(f"  {ticker} is not tradable on Alpaca, skipping.\n")
            results.append((ticker, None))
            continue

        try:
            ask = get_latest_ask(ticker)
        except Exception as e:
            print(f"  Could not fetch quote for {ticker}: {e}\n")
            results.append((ticker, None))
            continue

        est_cost = ask * qty_per_trade * 1.01  # 1% slippage buffer
        if est_cost > budget:
            print(f"  Est. cost ${est_cost:,.2f} (ask ${ask:,.2f}) exceeds remaining budget ${budget:,.2f}, skipping.\n")
            results.append((ticker, None))
            continue

        try:
            result = buy_with_trailing_stop(ticker, qty_per_trade, trail_percent=trail_percent)
            results.append((ticker, result))
            if result and result[0] is not None:
                placed += 1
                budget -= est_cost
                print(f"  Remaining budget: ${budget:,.2f}\n")
        except Exception as e:
            print(f"  ERROR on {ticker}: {e}\n")
            results.append((ticker, None))

    print(f"=== Done: {placed} / {len(unique_buys)} trades placed (${buying_power - budget:,.2f} committed) ===")
    return results


def get_positions():
    """Show all current positions."""
    positions = client.get_all_positions()
    if not positions:
        print("No open positions.")
        return positions

    print(f"{'Symbol':<8} {'Qty':>5} {'Avg Entry':>10} {'Current':>10} {'P/L $':>10} {'P/L %':>8}")
    print("-" * 55)
    for p in positions:
        print(
            f"{p.symbol:<8} "
            f"{p.qty:>5} "
            f"${float(p.avg_entry_price):>9,.2f} "
            f"${float(p.current_price):>9,.2f} "
            f"${float(p.unrealized_pl):>9,.2f} "
            f"{float(p.unrealized_plpc) * 100:>7.2f}%"
        )
    return positions


def get_open_orders():
    """Show all open/pending orders."""
    request = GetOrdersRequest(status=QueryOrderStatus.OPEN)
    orders = client.get_orders(request)
    if not orders:
        print("No open orders.")
        return orders

    print(f"{'Symbol':<8} {'Side':<6} {'Type':<16} {'Qty':>5} {'Status':<12} {'Trail':>8}  ID")
    print("-" * 80)
    for o in orders:
        trail = ""
        if o.trail_percent:
            trail = f"{o.trail_percent}%"
        elif o.trail_price:
            trail = f"${o.trail_price}"
        print(
            f"{o.symbol:<8} "
            f"{o.side.value:<6} "
            f"{o.type.value:<16} "
            f"{o.qty:>5} "
            f"{o.status.value:<12} "
            f"{trail:>8}  "
            f"{o.id}"
        )
    return orders


if __name__ == "__main__":
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "status":
        print("=== Account Info ===")
        get_account_info()
        print("\n=== Open Positions ===")
        get_positions()
        print("\n=== Open Orders ===")
        get_open_orders()
    elif cmd == "buy":
        symbol = sys.argv[2] if len(sys.argv) > 2 else "AAPL"
        qty = float(sys.argv[3]) if len(sys.argv) > 3 else 1
        trail = float(sys.argv[4]) if len(sys.argv) > 4 else 5.0
        buy_with_trailing_stop(symbol, qty, trail_percent=trail)
    elif cmd == "sell":
        symbol = sys.argv[2] if len(sys.argv) > 2 else "AAPL"
        qty = float(sys.argv[3]) if len(sys.argv) > 3 else 1
        submit_order(symbol, qty, OrderSide.SELL)
    elif cmd == "ladder":
        # python3 trade.py ladder AAPL 10 5 2.0 [trail%]
        symbol = sys.argv[2] if len(sys.argv) > 2 else "AAPL"
        total_qty = int(sys.argv[3]) if len(sys.argv) > 3 else 10
        steps = int(sys.argv[4]) if len(sys.argv) > 4 else 5
        step_pct = float(sys.argv[5]) if len(sys.argv) > 5 else 2.0
        trail = float(sys.argv[6]) if len(sys.argv) > 6 else None
        ladder_buy(symbol, total_qty, steps, step_pct, trail_percent=trail)
    elif cmd == "congress":
        # python3 trade.py congress [buys|sells|all|mirror] [days] [qty] [trail%]
        mode = sys.argv[2] if len(sys.argv) > 2 else "all"
        days = int(sys.argv[3]) if len(sys.argv) > 3 else 15

        if mode == "mirror":
            qty = int(sys.argv[4]) if len(sys.argv) > 4 else 1
            trail = float(sys.argv[5]) if len(sys.argv) > 5 else 5.0
            max_spend = float(sys.argv[6]) if len(sys.argv) > 6 else None
            mirror_congress_buys(qty_per_trade=qty, trail_percent=trail, days=days, max_spend=max_spend)
        else:
            from capitol import get_congress_buys, get_congress_sells, fetch_trades_since, print_trades, print_summary
            date_field = "pub_date"
            if mode == "buys":
                print(f"=== Congressional BUYS published in the last {days} days ===\n")
                trades = get_congress_buys(days, date_field=date_field)
            elif mode == "sells":
                print(f"=== Congressional SELLS published in the last {days} days ===\n")
                trades = get_congress_sells(days, date_field=date_field)
            else:
                print(f"=== All Congressional Trades published in the last {days} days ===\n")
                trades = fetch_trades_since(days, date_field=date_field)
            print_trades(trades)
            print_summary(trades)
    elif cmd == "history":
        # python3 trade.py history [ticker|politician|stats|fetches] [value] [days]
        from db import query_trades, get_fetch_history, get_stats
        from capitol import print_trades, print_summary
        mode = sys.argv[2] if len(sys.argv) > 2 else "stats"

        if mode == "stats":
            s = get_stats()
            print("=== Database Stats ===")
            print(f"  Total trades:      {s['total_trades']}")
            print(f"  Buys / Sells:      {s['total_buys']} / {s['total_sells']}")
            print(f"  Unique tickers:    {s['unique_tickers']}")
            print(f"  Unique politicians:{s['unique_politicians']}")
            print(f"  Date range:        {s['earliest_date']} to {s['latest_date']}")
            print(f"  Total fetches:     {s['total_fetches']}")
            print(f"  Last fetch:        {s['last_fetch']}")

        elif mode == "fetches":
            fetches = get_fetch_history(20)
            print("=== Fetch History ===")
            print(f"{'When':<22} {'Days':>5} {'Pages':>6} {'New':>5} {'Total':>6}")
            print("-" * 50)
            for f in fetches:
                print(f"{f['fetched_at'][:19]:<22} {f['days_back']:>5} {f['pages']:>6} {f['new_trades']:>5} {f['total_trades']:>6}")

        elif mode == "ticker":
            ticker = sys.argv[3] if len(sys.argv) > 3 else "AAPL"
            days = int(sys.argv[4]) if len(sys.argv) > 4 else None
            trades = query_trades(ticker=ticker, days=days)
            print(f"=== Stored trades for {ticker.upper()} ===\n")
            print_trades(trades)
            print_summary(trades)

        elif mode == "politician":
            name = sys.argv[3] if len(sys.argv) > 3 else ""
            days = int(sys.argv[4]) if len(sys.argv) > 4 else None
            trades = query_trades(politician=name, days=days)
            print(f"=== Stored trades for '{name}' ===\n")
            print_trades(trades)
            print_summary(trades)

        elif mode == "buys":
            days = int(sys.argv[3]) if len(sys.argv) > 3 else None
            trades = query_trades(tx_type="buy", days=days)
            print("=== Stored BUY trades ===\n")
            print_trades(trades)
            print_summary(trades)

        elif mode == "sells":
            days = int(sys.argv[3]) if len(sys.argv) > 3 else None
            trades = query_trades(tx_type="sell", days=days)
            print("=== Stored SELL trades ===\n")
            print_trades(trades)
            print_summary(trades)

        else:
            print("Usage: python3 trade.py history [stats|fetches|ticker|politician|buys|sells]")
    else:
        print("Usage:")
        print("  python3 trade.py status")
        print("  python3 trade.py buy [symbol] [qty] [trail%]")
        print("  python3 trade.py sell [symbol] [qty]")
        print("  python3 trade.py ladder [symbol] [total_qty] [steps] [step%] [trail%]")
        print("  python3 trade.py congress [buys|sells|all] [days]        # fetch & store (default: 15 days)")
        print("  python3 trade.py congress mirror [days] [qty] [trail%] [max_spend$]   # mirror buys (whole shares only)")
        print("  python3 trade.py history [stats|fetches|ticker|politician|buys|sells]")
