import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date
from decimal import Decimal
from pathlib import Path

from fx_tracker.database import (
    add_product,
    connect,
    get_exchange_rates,
    get_price_history,
    initialize_database,
    save_exchange_rates,
    save_price_history,
)
from fx_tracker.exchange_rates import ExchangeRate


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.sqlite3"
        initialize_database(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_saves_and_reads_rates_with_unique_currency_and_actual_rate_date(self):
        rate = ExchangeRate("USD", date(2026, 10, 16), Decimal("1400.123456"))
        first = save_exchange_rates(self.db_path, [rate])
        second = save_exchange_rates(self.db_path, [rate])
        rows = get_exchange_rates(self.db_path, currency="usd")
        self.assertEqual(first, second)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["rate_date"], "2026-10-16")
        self.assertEqual(rows[0]["currency"], "USD")

    def test_saves_product_and_converted_price_history(self):
        rate_id = save_exchange_rates(
            self.db_path, [ExchangeRate("JPY", date(2026, 10, 16), Decimal("9.2"))]
        )[0]
        product_id = add_product(
            self.db_path, name="Sample", currency="jpy", original_price=Decimal("5000"),
            url="https://example.test/item", image_path="images/sample.png"
        )
        history_id = save_price_history(
            self.db_path, product_id=product_id, exchange_rate_id=rate_id,
            history_date=date(2026, 10, 16), price_used=Decimal("5000"),
            exchange_rate_used=Decimal("9.2")
        )
        history = get_price_history(self.db_path, product_id)
        self.assertEqual(history[0]["id"], history_id)
        self.assertEqual(history[0]["converted_price"], 46000)

    def test_database_rejects_duplicate_price_history_and_invalid_foreign_keys(self):
        rate_id = save_exchange_rates(
            self.db_path, [ExchangeRate("EUR", date(2026, 10, 16), Decimal("1600"))]
        )[0]
        product_id = add_product(self.db_path, name="Sample", currency="EUR", original_price=Decimal("10"))
        args = dict(product_id=product_id, exchange_rate_id=rate_id, history_date=date(2026, 10, 16),
                    price_used=Decimal("10"), exchange_rate_used=Decimal("1600"))
        save_price_history(self.db_path, **args)
        with self.assertRaises(sqlite3.IntegrityError):
            save_price_history(self.db_path, **args)
        with self.assertRaises(sqlite3.IntegrityError):
            save_price_history(self.db_path, **{**args, "product_id": 999})

    def test_foreign_keys_are_enabled_for_every_connection(self):
        with closing(connect(self.db_path)) as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
