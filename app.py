"""Flask UI for the congressional trade mirroring tool.

Binds to 127.0.0.1 only. Reuses trade.py / capitol.py / db.py — no trade
logic is duplicated here.

Long-running actions (mirror, buy-with-stop, capitol refresh) are executed
on background threads; the UI polls /api/jobs/<id> for streaming stdout.
"""

import io
import os
import threading
import uuid
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, render_template, request

from alpaca.trading.enums import OrderSide, QueryOrderStatus
from alpaca.trading.requests import GetCalendarRequest, GetOrdersRequest

from trade import (
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY,
    IS_PAPER_TRADING,
    PDT_DAYTRADE_LIMIT,
    PDT_EQUITY_MIN,
    buy_with_trailing_stop,
    client,
    get_latest_ask,
    is_market_open,
    is_tradable,
    mirror_congress_buys,
    pdt_block_reason,
    submit_order,
)
from capitol import fetch_trades_since
from db import (
    get_stats,
    latest_buy_fills_by_symbol,
    query_alpaca_orders,
    query_trades,
    upsert_alpaca_orders,
)


app = Flask(__name__)


# ---------------------------------------------------------------------------
# Background job registry — used for operations that can take >1 second so
# the browser can poll for streaming stdout instead of holding a request open.
# ---------------------------------------------------------------------------

class _StreamBuffer(io.TextIOBase):
    """Thread-safe stdout sink that the polling endpoint can read mid-run."""

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._chunks: list[str] = []

    def write(self, s: str) -> int:
        with self._lock:
            self._chunks.append(s)
        return len(s)

    def getvalue(self) -> str:
        with self._lock:
            return "".join(self._chunks)


_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()


def _start_job(fn, *args, **kwargs) -> str:
    job_id = uuid.uuid4().hex[:8]
    buf = _StreamBuffer()
    with _jobs_lock:
        _jobs[job_id] = {"status": "running", "buffer": buf, "error": None}

    def _runner():
        import contextlib
        try:
            with contextlib.redirect_stdout(buf):
                fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 — we want to surface any failure to the UI
            buf.write(f"\n[ERROR] {e}\n")
            with _jobs_lock:
                _jobs[job_id]["error"] = str(e)
        finally:
            # Any buy/sell/mirror/refresh likely changed order state — pull fresh
            # closed-orders snapshot before the UI polls next.
            try:
                sync_orders_to_db()
            except Exception as e:  # noqa: BLE001
                buf.write(f"\n[sync warning] {e}\n")
            with _jobs_lock:
                _jobs[job_id]["status"] = "done"

    threading.Thread(target=_runner, daemon=True).start()
    return job_id


# ---------------------------------------------------------------------------
# Active window — polling (UI + background sync) is gated to a window around
# the market session: [open - ACTIVE_BEFORE_MINUTES, close + ACTIVE_AFTER_MINUTES].
# Outside that window Alpaca calls are paused; nothing is changing anyway.
# ---------------------------------------------------------------------------

ACTIVE_BEFORE_MINUTES = 30
ACTIVE_AFTER_MINUTES = 30
ET = ZoneInfo("America/New_York")

_session_cache: dict[str, Any] = {"fetched_date": None, "sessions": []}
_session_lock = threading.Lock()


def _refresh_session_cache() -> None:
    """Refresh the local cache of market sessions (today + next 7 days).

    One Alpaca Calendar call per day is sufficient; we use the cached sessions
    for every ``active_window_info`` query after that.
    """
    today = datetime.now(ET).date()
    with _session_lock:
        if _session_cache["fetched_date"] == today:
            return
        try:
            days: Any = client.get_calendar(
                GetCalendarRequest(start=today, end=today + timedelta(days=7))
            )
        except Exception as e:
            print(f"[calendar] fetch failed: {e}")
            return
        _session_cache["fetched_date"] = today
        _session_cache["sessions"] = list(days) if days else []


def _current_or_next_session():
    """Return the session whose post-close window still includes now, or the next upcoming."""
    now = datetime.now(ET)
    for s in _session_cache["sessions"]:
        # s.close is tz-aware (ET). Keep this session until the post-close buffer passes.
        close = s.close
        if close.tzinfo is None:
            close = close.replace(tzinfo=ET)
        if close + timedelta(minutes=ACTIVE_AFTER_MINUTES) >= now:
            return s
    return None


def active_window_info() -> dict:
    """Return the current polling-window state. No Alpaca call after the cache is warm."""
    _refresh_session_cache()
    s = _current_or_next_session()
    if s is None:
        return {
            "active": False,
            "reason": "no_upcoming_session",
            "window_start": None,
            "window_end": None,
            "market_open": None,
            "market_close": None,
            "now": datetime.now(ET).isoformat(),
        }
    open_dt = s.open if s.open.tzinfo else s.open.replace(tzinfo=ET)
    close_dt = s.close if s.close.tzinfo else s.close.replace(tzinfo=ET)
    start = open_dt - timedelta(minutes=ACTIVE_BEFORE_MINUTES)
    end = close_dt + timedelta(minutes=ACTIVE_AFTER_MINUTES)
    now = datetime.now(ET)
    if start <= now <= end:
        reason = "in_window"
        active = True
    elif now < start:
        reason = "before_window"
        active = False
    else:
        reason = "after_window"
        active = False
    return {
        "active": active,
        "reason": reason,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "market_open": open_dt.isoformat(),
        "market_close": close_dt.isoformat(),
        "now": now.isoformat(),
    }


def in_active_window() -> bool:
    return active_window_info()["active"]


# ---------------------------------------------------------------------------
# Alpaca order cache — pulls closed orders into SQLite so the UI / acquisition
# lookups don't hit Alpaca on every refresh.
# ---------------------------------------------------------------------------

SYNC_INTERVAL_SECONDS = 60
_sync_lock = threading.Lock()


def _iso(dt):
    return dt.isoformat() if dt is not None else None


def _order_to_row(o: Any) -> dict:
    return {
        "id": str(o.id),
        "client_order_id": getattr(o, "client_order_id", None),
        "symbol": o.symbol.upper(),
        "side": o.side.value,
        "type": o.type.value,
        "order_class": o.order_class.value if getattr(o, "order_class", None) else None,
        "qty": _f(o.qty) if o.qty is not None else None,
        "filled_qty": _f(getattr(o, "filled_qty", None)),
        "filled_avg_price": _f(getattr(o, "filled_avg_price", None)) if getattr(o, "filled_avg_price", None) else None,
        "limit_price": _f(o.limit_price) if getattr(o, "limit_price", None) else None,
        "stop_price": _f(o.stop_price) if getattr(o, "stop_price", None) else None,
        "trail_percent": _f(o.trail_percent) if getattr(o, "trail_percent", None) else None,
        "trail_price": _f(o.trail_price) if getattr(o, "trail_price", None) else None,
        "hwm": _f(getattr(o, "hwm", None)) if getattr(o, "hwm", None) else None,
        "time_in_force": o.time_in_force.value if getattr(o, "time_in_force", None) else None,
        "status": o.status.value,
        "submitted_at": _iso(getattr(o, "submitted_at", None)),
        "filled_at": _iso(getattr(o, "filled_at", None)),
        "canceled_at": _iso(getattr(o, "canceled_at", None)),
        "expired_at": _iso(getattr(o, "expired_at", None)),
    }


def sync_orders_to_db(limit: int = 500) -> int:
    """Pull the most recent closed orders from Alpaca and upsert them locally.

    Serialized by ``_sync_lock`` so background + post-job triggers don't
    collide. Returns the number of rows upserted.
    """
    if not _sync_lock.acquire(blocking=False):
        return 0
    try:
        req = GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=limit)
        orders: Any = client.get_orders(req)
        rows = [_order_to_row(o) for o in orders]
        return upsert_alpaca_orders(rows)
    finally:
        _sync_lock.release()


def _sync_loop():
    import time
    while True:
        time.sleep(SYNC_INTERVAL_SECONDS)
        if not in_active_window():
            continue  # market asleep — no new fills to sync
        try:
            sync_orders_to_db()
        except Exception as e:  # noqa: BLE001
            print(f"[sync] background sync failed: {e}")


def start_background_sync():
    threading.Thread(target=_sync_loop, daemon=True).start()


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", mode="paper" if IS_PAPER_TRADING else "live")


# ---------------------------------------------------------------------------
# Read-only snapshots
# ---------------------------------------------------------------------------

def _f(val, default: float = 0.0) -> float:
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


@app.route("/api/account")
def api_account():
    acct: Any = client.get_account()
    equity = _f(getattr(acct, "equity", 0))
    last_equity = _f(getattr(acct, "last_equity", 0))
    return jsonify({
        "mode": "paper" if IS_PAPER_TRADING else "live",
        "status": str(getattr(acct, "status", "")),
        "buying_power": _f(getattr(acct, "buying_power", 0)),
        "cash": _f(getattr(acct, "cash", 0)),
        "portfolio_value": _f(getattr(acct, "portfolio_value", 0)),
        "equity": equity,
        "last_equity": last_equity,
        "day_pl": equity - last_equity,
        "market_open": is_market_open(),
        "daytrade_count": int(getattr(acct, "daytrade_count", 0) or 0),
        "pattern_day_trader": bool(getattr(acct, "pattern_day_trader", False)),
        "pdt_equity_min": PDT_EQUITY_MIN,
        "pdt_daytrade_limit": PDT_DAYTRADE_LIMIT,
        "pdt_block_reason": pdt_block_reason(acct),
    })


def _acquisition_dates(symbols: list[str]) -> dict[str, str]:
    """Return {symbol: ISO timestamp of the most recent filled BUY} for the given symbols.

    Reads from the local alpaca_orders cache (populated by sync_orders_to_db).
    No Alpaca API call — this used to fire on every positions refresh.
    """
    return latest_buy_fills_by_symbol(symbols)


@app.route("/api/positions")
def api_positions():
    positions: Any = client.get_all_positions()
    symbols = [p.symbol.upper() for p in positions]
    try:
        acquired = _acquisition_dates(symbols)
    except Exception:
        acquired = {}
    return jsonify([
        {
            "symbol": p.symbol,
            "qty": _f(p.qty),
            "avg_entry_price": _f(p.avg_entry_price),
            "current_price": _f(p.current_price),
            "market_value": _f(p.market_value),
            "unrealized_pl": _f(p.unrealized_pl),
            "unrealized_plpc": _f(p.unrealized_plpc),
            "acquired_at": acquired.get(p.symbol.upper()),
        }
        for p in positions
    ])


@app.route("/api/orders")
def api_orders():
    orders: Any = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
    out = []
    for o in orders:
        out.append({
            "id": str(o.id),
            "symbol": o.symbol,
            "side": o.side.value,
            "type": o.type.value,
            "qty": _f(o.qty),
            "status": o.status.value,
            "trail_percent": _f(o.trail_percent) if o.trail_percent else None,
            "trail_price": _f(o.trail_price) if o.trail_price else None,
            "limit_price": _f(o.limit_price) if o.limit_price else None,
            "submitted_at": o.submitted_at.isoformat() if o.submitted_at else None,
        })
    return jsonify(out)


_TRADING_BASE = "https://paper-api.alpaca.markets" if IS_PAPER_TRADING else "https://api.alpaca.markets"
_TIMEFRAME_FOR_PERIOD = {
    "1D": "5Min",
    "1W": "15Min",
    "1M": "1H",
    "3M": "1D",
    "1Y": "1D",
    "all": "1D",
}


DEFAULT_MARKET_SYMBOLS = "SPY,QQQ,DIA,IWM"
_MARKET_LABELS = {
    "SPY": "S&P 500",
    "QQQ": "Nasdaq 100",
    "DIA": "Dow 30",
    "IWM": "Russell 2000",
    "VIXY": "Volatility",
    "TLT": "20Y Treasuries",
    "GLD": "Gold",
    "UUP": "US Dollar",
}


@app.route("/api/market/snapshots")
def api_market_snapshots():
    """Multi-symbol snapshot for a broad-market glance strip.

    Defaults to the four major index ETFs. Callers can override with
    ?symbols=SPY,QQQ,...  — used by the Markets widget in the UI.
    """
    symbols = request.args.get("symbols", DEFAULT_MARKET_SYMBOLS).upper()
    headers = {
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }
    try:
        resp = requests.get(
            "https://data.alpaca.markets/v2/stocks/snapshots",
            headers=headers,
            params={"symbols": symbols},
            timeout=10,
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    if resp.status_code != 200:
        return jsonify({"error": resp.text}), resp.status_code

    out = []
    for sym, snap in (resp.json() or {}).items():
        latest = (snap.get("latestTrade") or {}).get("p")
        daily = snap.get("dailyBar") or {}
        prev = snap.get("prevDailyBar") or {}
        last = latest if latest is not None else daily.get("c")
        prev_close = prev.get("c")
        change = (last - prev_close) if (last is not None and prev_close) else None
        change_pct = (change / prev_close * 100) if (change is not None and prev_close) else None
        out.append({
            "symbol": sym,
            "label": _MARKET_LABELS.get(sym, sym),
            "last": last,
            "prev_close": prev_close,
            "open": daily.get("o"),
            "high": daily.get("h"),
            "low": daily.get("l"),
            "volume": daily.get("v"),
            "change": change,
            "change_pct": change_pct,
            "as_of": (snap.get("latestTrade") or {}).get("t"),
        })
    # Preserve the requested symbol order
    order = {s: i for i, s in enumerate(symbols.split(","))}
    out.sort(key=lambda r: order.get(r["symbol"], 999))
    return jsonify(out)


@app.route("/api/portfolio-history")
def api_portfolio_history():
    """Proxy Alpaca's /v2/account/portfolio/history — equity time series for the chart."""
    period = request.args.get("period", "1M")
    timeframe = request.args.get("timeframe") or _TIMEFRAME_FOR_PERIOD.get(period, "1D")
    headers = {
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }
    resp = requests.get(
        f"{_TRADING_BASE}/v2/account/portfolio/history",
        headers=headers,
        params={"period": period, "timeframe": timeframe},
        timeout=10,
    )
    if resp.status_code != 200:
        return jsonify({"error": resp.text}), resp.status_code
    return jsonify(resp.json())


@app.route("/api/status")
def api_status():
    """Lightweight — tells the UI whether to poll. No Alpaca call."""
    return jsonify(active_window_info())


@app.route("/api/stats")
def api_stats():
    return jsonify(get_stats())


@app.route("/api/history/orders")
def api_history_orders():
    """Cached closed orders. Defaults to sells since that's the history view."""
    side = request.args.get("side", "sell")
    if side == "all":
        side = None
    days = request.args.get("days")
    days = int(days) if days else None
    symbol = request.args.get("symbol") or None
    limit = int(request.args.get("limit", 100))
    orders = query_alpaca_orders(side=side, symbol=symbol, days=days, limit=limit)
    return jsonify(orders)


@app.route("/api/history/sync", methods=["POST"])
def api_history_sync():
    """Manual trigger — UI "Sync" button hits this to force a refresh."""
    try:
        n = sync_orders_to_db()
        return jsonify({"ok": True, "synced": n})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/congress")
def api_congress():
    mode = request.args.get("mode", "all")
    days = int(request.args.get("days", 15))
    filters: dict = {"days": days}
    if mode == "buys":
        filters["tx_type"] = "buy"
    elif mode == "sells":
        filters["tx_type"] = "sell"
    trades = query_trades(**filters)
    # Trim: UI only needs the first ~200 rows.
    return jsonify(trades[:200])


@app.route("/api/quote/<symbol>")
def api_quote(symbol):
    try:
        ask = get_latest_ask(symbol)
        tradable = is_tradable(symbol)
        return jsonify({"symbol": symbol.upper(), "ask": ask, "tradable": tradable})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ---------------------------------------------------------------------------
# Instant actions (fast enough to run synchronously)
# ---------------------------------------------------------------------------

@app.route("/api/sell", methods=["POST"])
def api_sell():
    data = request.get_json(force=True)
    symbol = str(data["symbol"]).upper()
    qty = float(data["qty"])
    buf = _StreamBuffer()
    import contextlib
    try:
        with contextlib.redirect_stdout(buf):
            submit_order(symbol, qty, OrderSide.SELL)
        return jsonify({"ok": True, "log": buf.getvalue()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "log": buf.getvalue()}), 400


@app.route("/api/orders/<order_id>/cancel", methods=["POST"])
def api_cancel_order(order_id):
    try:
        client.cancel_order_by_id(order_id)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


# ---------------------------------------------------------------------------
# Long-running jobs
# ---------------------------------------------------------------------------

@app.route("/api/jobs/buy", methods=["POST"])
def api_job_buy():
    data = request.get_json(force=True)
    symbol = str(data["symbol"]).upper()
    qty = int(data["qty"])
    trail_percent = float(data["trail_percent"])
    job_id = _start_job(buy_with_trailing_stop, symbol, qty, trail_percent=trail_percent)
    return jsonify({"job_id": job_id})


@app.route("/api/jobs/mirror", methods=["POST"])
def api_job_mirror():
    data = request.get_json(force=True)
    days = int(data.get("days", 15))
    qty = int(data.get("qty", 1))
    trail = float(data.get("trail_percent", 5.0))
    raw_max = data.get("max_spend")
    max_spend = float(raw_max) if raw_max not in (None, "") else None
    job_id = _start_job(
        mirror_congress_buys,
        qty_per_trade=qty,
        trail_percent=trail,
        days=days,
        max_spend=max_spend,
    )
    return jsonify({"job_id": job_id})


@app.route("/api/jobs/refresh-congress", methods=["POST"])
def api_job_refresh_congress():
    data = request.get_json(force=True) or {}
    days = int(data.get("days", 15))
    job_id = _start_job(fetch_trades_since, days, date_field="pub_date")
    return jsonify({"job_id": job_id})


@app.route("/api/jobs/<job_id>")
def api_job_status(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    return jsonify({
        "status": job["status"],
        "error": job["error"],
        "log": job["buffer"].getvalue(),
    })


def _startup_once():
    """Prime the order cache and start the background sync thread.

    Runs at module import so every entry point (``python app.py``, gunicorn,
    ``flask run``) triggers it. Guarded to run exactly once per process.
    """
    if getattr(_startup_once, "_done", False):
        return
    _startup_once._done = True
    try:
        n = sync_orders_to_db()
        print(f"[sync] startup: cached {n} orders locally")
    except Exception as e:
        print(f"[sync] startup sync failed: {e}")
    start_background_sync()


_startup_once()


if __name__ == "__main__":
    # HOST defaults to localhost for local dev; override with HOST=0.0.0.0 in Docker.
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "5000"))
    app.run(host=host, port=port, debug=False, threaded=True)
