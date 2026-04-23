"""SQLite database for persisting congressional trades and fetch history."""

import os
import sqlite3
from datetime import datetime

DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "trades.db"))


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS congress_trades (
            tx_id       TEXT PRIMARY KEY,
            tx_date     TEXT NOT NULL,
            pub_date    TEXT,
            tx_type     TEXT NOT NULL,
            ticker      TEXT NOT NULL,
            company     TEXT,
            politician  TEXT NOT NULL,
            party       TEXT,
            value       INTEGER,
            first_seen  TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_trades_date ON congress_trades(tx_date);
        CREATE INDEX IF NOT EXISTS idx_trades_ticker ON congress_trades(ticker);
        CREATE INDEX IF NOT EXISTS idx_trades_politician ON congress_trades(politician);
        CREATE INDEX IF NOT EXISTS idx_trades_type ON congress_trades(tx_type);

        CREATE TABLE IF NOT EXISTS fetch_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at  TEXT NOT NULL,
            days_back   INTEGER NOT NULL,
            pages       INTEGER NOT NULL,
            new_trades  INTEGER NOT NULL,
            total_trades INTEGER NOT NULL
        );

        -- Cached snapshot of Alpaca orders. Rows are upserted by Alpaca order id;
        -- once an order is final (filled/canceled/expired/rejected) its state never
        -- changes, so reading from here instead of Alpaca saves API calls.
        CREATE TABLE IF NOT EXISTS alpaca_orders (
            id               TEXT PRIMARY KEY,
            client_order_id  TEXT,
            symbol           TEXT NOT NULL,
            side             TEXT NOT NULL,
            type             TEXT NOT NULL,
            order_class      TEXT,
            qty              REAL,
            filled_qty       REAL,
            filled_avg_price REAL,
            limit_price      REAL,
            stop_price       REAL,
            trail_percent    REAL,
            trail_price      REAL,
            hwm              REAL,
            time_in_force    TEXT,
            status           TEXT NOT NULL,
            submitted_at     TEXT,
            filled_at        TEXT,
            canceled_at      TEXT,
            expired_at       TEXT,
            last_synced      TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_orders_symbol     ON alpaca_orders(symbol);
        CREATE INDEX IF NOT EXISTS idx_orders_side       ON alpaca_orders(side);
        CREATE INDEX IF NOT EXISTS idx_orders_status     ON alpaca_orders(status);
        CREATE INDEX IF NOT EXISTS idx_orders_filled_at  ON alpaca_orders(filled_at);
        CREATE INDEX IF NOT EXISTS idx_orders_submitted  ON alpaca_orders(submitted_at);
    """)
    conn.commit()
    conn.close()


def upsert_trades(trades):
    """Insert trades, skipping duplicates. Returns count of new trades."""
    conn = get_conn()
    now = datetime.now().isoformat()
    new_count = 0
    for t in trades:
        try:
            conn.execute(
                """INSERT INTO congress_trades
                   (tx_id, tx_date, pub_date, tx_type, ticker, company, politician, party, value, first_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (t["tx_id"], t["date"], t["pub_date"], t["type"], t["ticker"],
                 t["company"], t["politician"], t["party"], t["value"], now),
            )
            new_count += 1
        except sqlite3.IntegrityError:
            pass  # already exists
    conn.commit()
    conn.close()
    return new_count


def log_fetch(days_back, pages, new_trades, total_trades):
    conn = get_conn()
    conn.execute(
        "INSERT INTO fetch_log (fetched_at, days_back, pages, new_trades, total_trades) VALUES (?, ?, ?, ?, ?)",
        (datetime.now().isoformat(), days_back, pages, new_trades, total_trades),
    )
    conn.commit()
    conn.close()


def query_trades(tx_type=None, ticker=None, politician=None, days=None, limit=None):
    """Query stored trades with optional filters."""
    conn = get_conn()
    sql = "SELECT * FROM congress_trades WHERE 1=1"
    params = []

    if tx_type:
        sql += " AND tx_type = ?"
        params.append(tx_type)
    if ticker:
        sql += " AND ticker = ?"
        params.append(ticker.upper())
    if politician:
        sql += " AND politician LIKE ?"
        params.append(f"%{politician}%")
    if days:
        from datetime import timedelta
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        sql += " AND tx_date >= ?"
        params.append(cutoff)

    sql += " ORDER BY tx_date DESC"
    if limit:
        sql += f" LIMIT {limit}"

    rows = conn.execute(sql, params).fetchall()
    conn.close()
    # Map db column names to the format print_trades expects
    trades = []
    for r in rows:
        d = dict(r)
        d["date"] = d.pop("tx_date")
        d["type"] = d.pop("tx_type")
        trades.append(d)
    return trades


def get_fetch_history(limit=10):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM fetch_log ORDER BY fetched_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


_ORDER_COLS = [
    "id", "client_order_id", "symbol", "side", "type", "order_class",
    "qty", "filled_qty", "filled_avg_price", "limit_price", "stop_price",
    "trail_percent", "trail_price", "hwm", "time_in_force", "status",
    "submitted_at", "filled_at", "canceled_at", "expired_at", "last_synced",
]


def upsert_alpaca_orders(orders):
    """Upsert a list of order dicts (see _ORDER_COLS). Returns count inserted-or-updated."""
    if not orders:
        return 0
    now = datetime.now().isoformat()
    placeholders = ",".join("?" for _ in _ORDER_COLS)
    sql = f"INSERT OR REPLACE INTO alpaca_orders ({','.join(_ORDER_COLS)}) VALUES ({placeholders})"
    conn = get_conn()
    rows = []
    for o in orders:
        rows.append(tuple(o.get(c) if c != "last_synced" else now for c in _ORDER_COLS))
    conn.executemany(sql, rows)
    conn.commit()
    conn.close()
    return len(rows)


def query_alpaca_orders(side=None, status=None, symbol=None, days=None, limit=200):
    """Query cached Alpaca orders with optional filters. Orders newest-first by submitted_at."""
    conn = get_conn()
    sql = "SELECT * FROM alpaca_orders WHERE 1=1"
    params = []
    if side:
        sql += " AND side = ?"
        params.append(side.lower())
    if status:
        sql += " AND status = ?"
        params.append(status.lower())
    if symbol:
        sql += " AND symbol = ?"
        params.append(symbol.upper())
    if days:
        from datetime import timedelta
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        sql += " AND submitted_at >= ?"
        params.append(cutoff)
    sql += " ORDER BY submitted_at DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def latest_buy_fills_by_symbol(symbols):
    """Return {symbol: filled_at ISO string} for the most recent filled BUY per symbol."""
    if not symbols:
        return {}
    conn = get_conn()
    placeholders = ",".join("?" for _ in symbols)
    sql = f"""
        SELECT symbol, MAX(filled_at) AS filled_at
        FROM alpaca_orders
        WHERE side = 'buy' AND status = 'filled' AND filled_at IS NOT NULL
          AND symbol IN ({placeholders})
        GROUP BY symbol
    """
    rows = conn.execute(sql, [s.upper() for s in symbols]).fetchall()
    conn.close()
    return {r["symbol"]: r["filled_at"] for r in rows}


def get_stats():
    """Get overall database stats."""
    conn = get_conn()
    stats = {}
    stats["total_trades"] = conn.execute("SELECT COUNT(*) FROM congress_trades").fetchone()[0]
    stats["total_buys"] = conn.execute("SELECT COUNT(*) FROM congress_trades WHERE tx_type='buy'").fetchone()[0]
    stats["total_sells"] = conn.execute("SELECT COUNT(*) FROM congress_trades WHERE tx_type='sell'").fetchone()[0]
    stats["unique_tickers"] = conn.execute("SELECT COUNT(DISTINCT ticker) FROM congress_trades").fetchone()[0]
    stats["unique_politicians"] = conn.execute("SELECT COUNT(DISTINCT politician) FROM congress_trades").fetchone()[0]

    row = conn.execute("SELECT MIN(tx_date), MAX(tx_date) FROM congress_trades").fetchone()
    stats["earliest_date"] = row[0]
    stats["latest_date"] = row[1]

    stats["total_fetches"] = conn.execute("SELECT COUNT(*) FROM fetch_log").fetchone()[0]
    last = conn.execute("SELECT fetched_at FROM fetch_log ORDER BY fetched_at DESC LIMIT 1").fetchone()
    stats["last_fetch"] = last[0] if last else None

    conn.close()
    return stats


# Initialize on import
init_db()
