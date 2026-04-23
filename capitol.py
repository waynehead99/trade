"""Scrape recent congressional stock trades from Capitol Trades."""

import json
import re
from datetime import datetime, timedelta
import requests
from db import upsert_trades, log_fetch


CAPITOL_TRADES_URL = "https://www.capitoltrades.com/trades"


def _parse_page(html, include_non_ticker=False):
    """Parse trade data from a Capitol Trades HTML page."""
    payloads = re.findall(r'<script>self\.__next_f\.push\((.*?)\)</script>', html, re.DOTALL)
    decoder = json.JSONDecoder()
    trades = []
    for payload in payloads:
        if "txType" not in payload or "txDate" not in payload:
            continue

        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue

        if not isinstance(chunk, list) or len(chunk) < 2 or not isinstance(chunk[1], str):
            continue

        decoded = chunk[1]
        marker = 'data":['
        start = decoded.find(marker)
        if start == -1:
            continue

        try:
            records, _ = decoder.raw_decode(decoded[start + len('data":'):])
        except json.JSONDecodeError:
            continue

        for record in records:
            if not isinstance(record, dict) or "_txId" not in record:
                continue

            issuer = record.get("issuer") or {}
            politician = record.get("politician") or {}
            ticker = issuer.get("issuerTicker")

            if not ticker and not include_non_ticker:
                continue

            first = politician.get("nickname") or politician.get("firstName") or ""
            last = politician.get("lastName") or ""
            pub_date = record.get("pubDate") or ""

            trades.append({
                "tx_id": str(record["_txId"]),
                "date": record.get("txDate"),
                "pub_date": pub_date[:10] if pub_date else None,
                "type": record.get("txType"),
                "ticker": ticker.replace(":US", "") if ticker else None,
                "company": issuer.get("issuerName"),
                "politician": f"{first} {last}".strip(),
                "party": politician.get("party"),
                "value": int(record.get("value") or 0),
            })

    return trades


def fetch_trades_since(days=30, max_pages=100, date_field="tx_date"):
    """Fetch all trades from the last N days, paginating automatically.

    Args:
        days: Number of days to look back.
        max_pages: Maximum pages to fetch from Capitol Trades.
        date_field: Which trade field to filter on: ``tx_date`` or ``pub_date``.

    Stores results in the database and logs the fetch.
    """
    if date_field not in {"tx_date", "pub_date"}:
        raise ValueError("date_field must be 'tx_date' or 'pub_date'")

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    all_trades = []
    pages_fetched = 0

    for page in range(1, max_pages + 1):
        resp = requests.get(f"{CAPITOL_TRADES_URL}?sortBy=-txDate&page={page}")
        resp.raise_for_status()
        page_trades = _parse_page(resp.text, include_non_ticker=True)
        pages_fetched = page

        if not page_trades:
            break

        for t in page_trades:
            trade_date = t["date"] if date_field == "tx_date" else t["pub_date"]
            if t.get("ticker") and trade_date and trade_date >= cutoff:
                all_trades.append(t)

        page_dates = [
            t["date"] if date_field == "tx_date" else t["pub_date"]
            for t in page_trades
            if (t["date"] if date_field == "tx_date" else t["pub_date"])
        ]
        if not page_dates:
            break

        oldest = min(page_dates)
        if oldest < cutoff:
            break

    # Persist to database
    new_count = upsert_trades(all_trades)
    log_fetch(days, pages_fetched, new_count, len(all_trades))
    if new_count > 0:
        print(f"[db] Stored {new_count} new trades ({len(all_trades)} total fetched)\n")

    all_trades.sort(
        key=lambda t: (
            t["date"] if date_field == "tx_date" else (t.get("pub_date") or ""),
            t["date"],
            t.get("tx_id", ""),
        ),
        reverse=True,
    )
    return all_trades


def get_congress_buys(days=30, date_field="tx_date"):
    """Get congressional buy trades from the last N days."""
    return [t for t in fetch_trades_since(days, date_field=date_field) if t["type"] == "buy"]


def get_congress_sells(days=30, date_field="tx_date"):
    """Get congressional sell trades from the last N days."""
    return [t for t in fetch_trades_since(days, date_field=date_field) if t["type"] == "sell"]


def print_trades(trades):
    """Pretty-print a list of trades."""
    if not trades:
        print("No trades found.")
        return

    print(
        f"{'Tx Date':<12} {'Pub Date':<12} {'Type':<6} {'Ticker':<8} "
        f"{'Politician':<25} {'Party':<5} {'Value':>10}  {'Company'}"
    )
    print("-" * 104)
    for t in trades:
        party_short = "R" if t["party"] == "republican" else "D" if t["party"] == "democrat" else t["party"]
        print(
            f"{t['date']:<12} "
            f"{(t.get('pub_date') or ''):<12} "
            f"{t['type']:<6} "
            f"{t['ticker']:<8} "
            f"{t['politician']:<25} "
            f"{party_short:<5} "
            f"${t['value']:>9,}  "
            f"{t['company']}"
        )


def print_summary(trades):
    """Print a summary breakdown of trades."""
    if not trades:
        return
    buys = [t for t in trades if t["type"] == "buy"]
    sells = [t for t in trades if t["type"] == "sell"]
    politicians = {}
    for t in trades:
        name = t["politician"]
        if name not in politicians:
            politicians[name] = {"buys": 0, "sells": 0, "party": t["party"]}
        if t["type"] == "buy":
            politicians[name]["buys"] += 1
        elif t["type"] == "sell":
            politicians[name]["sells"] += 1

    print(f"\n{'='*40}")
    print(f"Total: {len(trades)} trades ({len(buys)} buys, {len(sells)} sells)")
    print(f"Politicians: {len(politicians)}")
    print(f"Tx date range: {min(t['date'] for t in trades)} to {max(t['date'] for t in trades)}")
    pub_dates = [t.get("pub_date") for t in trades if t.get("pub_date")]
    if pub_dates:
        print(f"Pub date range: {min(pub_dates)} to {max(pub_dates)}")
    print(f"\nBy politician:")
    for name in sorted(politicians, key=lambda n: politicians[n]["buys"] + politicians[n]["sells"], reverse=True):
        p = politicians[name]
        party = "R" if p["party"] == "republican" else "D"
        print(f"  {name:<25} {party}  {p['buys']}B / {p['sells']}S")


if __name__ == "__main__":
    import sys

    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 30

    if mode == "buys":
        print(f"=== Congressional BUYS (last {days} days) ===\n")
        trades = get_congress_buys(days)
    elif mode == "sells":
        print(f"=== Congressional SELLS (last {days} days) ===\n")
        trades = get_congress_sells(days)
    else:
        print(f"=== All Congressional Trades (last {days} days) ===\n")
        trades = fetch_trades_since(days)

    print_trades(trades)
    print_summary(trades)
