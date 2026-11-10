"""SQLite schema and persistence helpers for the FX tracker MVP."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable

from .exchange_rates import ExchangeRate


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    url VARCHAR(2048),
    currency CHAR(3) NOT NULL CHECK (currency = upper(currency)),
    original_price DECIMAL(12, 2) NOT NULL CHECK (original_price >= 0),
    image_path VARCHAR(2048),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exchange_rates (
    id INTEGER PRIMARY KEY,
    currency CHAR(3) NOT NULL CHECK (currency = upper(currency)),
    rate_date DATE NOT NULL,
    krw_rate DECIMAL(12, 6) NOT NULL CHECK (krw_rate > 0),
    source VARCHAR(100) NOT NULL,
    fetched_at TEXT NOT NULL,
    UNIQUE (currency, rate_date)
);
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    exchange_rate_id INTEGER NOT NULL REFERENCES exchange_rates(id) ON DELETE RESTRICT,
    history_date DATE NOT NULL,
    price_used DECIMAL(12, 2) NOT NULL CHECK (price_used >= 0),
    exchange_rate_used DECIMAL(12, 6) NOT NULL CHECK (exchange_rate_used > 0),
    converted_price DECIMAL(12, 2) NOT NULL CHECK (converted_price >= 0),
    created_at TEXT NOT NULL,
    UNIQUE (product_id, history_date)
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database(db_path: str | Path) -> None:
    """Create the MVP tables if absent; existing tables and data are retained."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(path)) as connection:
        connection.executescript(SCHEMA)


def save_exchange_rates(
    db_path: str | Path,
    rates: Iterable[ExchangeRate],
    *,
    fetched_at: datetime | None = None,
) -> list[int]:
    """Insert rates without replacing existing currency/date observations; return row IDs."""
    timestamp = (fetched_at or datetime.now(timezone.utc)).isoformat()
    ids: list[int] = []
    with closing(connect(db_path)) as connection, connection:
        for rate in rates:
            connection.execute(
                """INSERT INTO exchange_rates (currency, rate_date, krw_rate, source, fetched_at)
                   VALUES (?, ?, ?, ?, ?) ON CONFLICT(currency, rate_date) DO NOTHING""",
                (rate.currency.upper(), rate.rate_date.isoformat(), str(rate.krw_rate), rate.source, timestamp),
            )
            row = connection.execute(
                "SELECT id FROM exchange_rates WHERE currency = ? AND rate_date = ?",
                (rate.currency.upper(), rate.rate_date.isoformat()),
            ).fetchone()
            ids.append(int(row["id"]))
    return ids


def get_exchange_rates(
    db_path: str | Path,
    *,
    currency: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list[str] = []
    if currency:
        clauses.append("currency = ?")
        params.append(currency.strip().upper())
    if start_date:
        clauses.append("rate_date >= ?")
        params.append(start_date.isoformat())
    if end_date:
        clauses.append("rate_date <= ?")
        params.append(end_date.isoformat())
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with closing(connect(db_path)) as connection:
        return list(connection.execute(f"SELECT * FROM exchange_rates{where} ORDER BY rate_date, currency", params))


def get_latest_exchange_rates(db_path: str | Path, *, as_of: date | None = None) -> list[sqlite3.Row]:
    """Return one most-recent, non-future observation for each stored currency."""
    cutoff = (as_of or date.today()).isoformat()
    with closing(connect(db_path)) as connection:
        return list(connection.execute(
            """SELECT er.* FROM exchange_rates er
               JOIN (
                   SELECT currency, MAX(rate_date) AS rate_date
                   FROM exchange_rates WHERE rate_date <= ? GROUP BY currency
               ) latest ON latest.currency = er.currency AND latest.rate_date = er.rate_date
               ORDER BY er.currency""",
            (cutoff,),
        ))


def add_product(
    db_path: str | Path,
    *,
    name: str,
    currency: str,
    original_price: Decimal,
    url: str | None = None,
    image_path: str | None = None,
    created_at: datetime | None = None,
) -> int:
    now = (created_at or datetime.now(timezone.utc)).isoformat()
    with closing(connect(db_path)) as connection, connection:
        cursor = connection.execute(
            """INSERT INTO products (name, url, currency, original_price, image_path, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (name, url, currency.strip().upper(), str(original_price), image_path, now, now),
        )
        return int(cursor.lastrowid)


def list_products(db_path: str | Path) -> list[sqlite3.Row]:
    """Return products in newest-first registration order."""
    with closing(connect(db_path)) as connection:
        return list(connection.execute("SELECT * FROM products ORDER BY created_at DESC, id DESC"))


def get_product(db_path: str | Path, product_id: int) -> sqlite3.Row | None:
    with closing(connect(db_path)) as connection:
        return connection.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()


def update_product(db_path: str | Path, product_id: int, **changes: object) -> bool:
    """Update only supplied fields and refresh updated_at; return whether a row existed."""
    allowed = {"name", "url", "currency", "original_price", "image_path"}
    values = {key: value for key, value in changes.items() if key in allowed}
    if not values:
        return get_product(db_path, product_id) is not None
    if "currency" in values and values["currency"] is not None:
        values["currency"] = str(values["currency"]).strip().upper()
    if "original_price" in values and values["original_price"] is not None:
        values["original_price"] = str(values["original_price"])
    values["updated_at"] = datetime.now(timezone.utc).isoformat()
    assignments = ", ".join(f"{column} = ?" for column in values)
    with closing(connect(db_path)) as connection, connection:
        cursor = connection.execute(
            f"UPDATE products SET {assignments} WHERE id = ?",
            (*values.values(), product_id),
        )
        return cursor.rowcount > 0


def delete_product(db_path: str | Path, product_id: int) -> str | None:
    """Delete the product (price_history cascades) and return its saved image path."""
    with closing(connect(db_path)) as connection, connection:
        row = connection.execute("SELECT image_path FROM products WHERE id = ?", (product_id,)).fetchone()
        if row is None:
            return None
        connection.execute("DELETE FROM products WHERE id = ?", (product_id,))
        return row["image_path"]


def save_price_history(
    db_path: str | Path,
    *,
    product_id: int,
    exchange_rate_id: int,
    history_date: date,
    price_used: Decimal,
    exchange_rate_used: Decimal,
) -> int:
    converted = (price_used * exchange_rate_used).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    now = datetime.now(timezone.utc).isoformat()
    with closing(connect(db_path)) as connection, connection:
        cursor = connection.execute(
            """INSERT INTO price_history
               (product_id, exchange_rate_id, history_date, price_used, exchange_rate_used,
                converted_price, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (product_id, exchange_rate_id, history_date.isoformat(), str(price_used),
             str(exchange_rate_used), str(converted), now),
        )
        return int(cursor.lastrowid)


def get_price_history(db_path: str | Path, product_id: int) -> list[sqlite3.Row]:
    with closing(connect(db_path)) as connection:
        return list(connection.execute(
            "SELECT * FROM price_history WHERE product_id = ? ORDER BY history_date", (product_id,)
        ))


def record_product_price(
    db_path: str | Path,
    product_id: int,
    *,
    history_date: date,
) -> tuple[sqlite3.Row, bool]:
    """Calculate and store today's snapshot using the newest rate not dated in the future."""
    with closing(connect(db_path)) as connection, connection:
        product = connection.execute(
            "SELECT id, currency, original_price FROM products WHERE id = ?", (product_id,)
        ).fetchone()
        if product is None:
            raise LookupError("product_not_found")

        rate_row = connection.execute(
            """SELECT id, rate_date, krw_rate FROM exchange_rates
               WHERE currency = ? AND rate_date <= ? ORDER BY rate_date DESC, id DESC LIMIT 1""",
            (product["currency"], history_date.isoformat()),
        ).fetchone()
        if rate_row is None:
            raise LookupError("exchange_rate_not_found")

        # 가격 계산은 공용 Decimal 함수를 사용해 다른 호출 경로와 같은 규칙을 적용합니다.
        local_price = Decimal(str(product["original_price"]))
        exchange_rate = Decimal(str(rate_row["krw_rate"]))
        from .pricing import calculate_krw_price

        converted_price = calculate_krw_price(local_price, exchange_rate)
        cursor = connection.execute(
            """INSERT INTO price_history
               (product_id, exchange_rate_id, history_date, price_used, exchange_rate_used,
                converted_price, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(product_id, history_date) DO NOTHING""",
            (product_id, rate_row["id"], history_date.isoformat(), str(local_price),
             str(exchange_rate), str(converted_price), datetime.now(timezone.utc).isoformat()),
        )
        saved = _read_price_history_row(connection, product_id, history_date)
        return saved, cursor.rowcount == 1


def _read_price_history_row(connection: sqlite3.Connection, product_id: int, history_date: date) -> sqlite3.Row:
    return connection.execute(
        """SELECT ph.*, er.rate_date AS exchange_rate_date, p.currency AS currency
           FROM price_history ph
           JOIN exchange_rates er ON er.id = ph.exchange_rate_id
           JOIN products p ON p.id = ph.product_id
           WHERE ph.product_id = ? AND ph.history_date = ?""",
        (product_id, history_date.isoformat()),
    ).fetchone()


def list_product_price_history(db_path: str | Path, product_id: int) -> list[sqlite3.Row]:
    """Read snapshots in calendar order, including the actual source-rate date."""
    with closing(connect(db_path)) as connection:
        return list(connection.execute(
            """SELECT ph.*, er.rate_date AS exchange_rate_date, p.currency AS currency
               FROM price_history ph
               JOIN exchange_rates er ON er.id = ph.exchange_rate_id
               JOIN products p ON p.id = ph.product_id
               WHERE ph.product_id = ? ORDER BY ph.history_date, ph.id""",
            (product_id,),
        ))
