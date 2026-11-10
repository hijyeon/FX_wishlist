import json
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from fx_tracker.database import get_latest_exchange_rates, save_exchange_rates
from fx_tracker.exchange_rates import ExchangeRate
from fx_tracker.main import create_app

from test_products_api import ASGIClient


class FrontendApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.upload_dir = root / "uploads"
        self.client = ASGIClient(create_app(root / "db.sqlite3", self.upload_dir))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_frontend_home_and_uploaded_images_are_served(self):
        status, html = self.client.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"FX Wishlist", html)

        image_bytes = b"\x89PNG\r\n\x1a\nexample"
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        (self.upload_dir / "sample.png").write_bytes(image_bytes)
        status, body = self.client.request("GET", "/uploads/sample.png")
        self.assertEqual(status, 200)
        self.assertEqual(body, image_bytes)

    def test_exchange_rate_list_shows_latest_real_record_for_each_currency(self):
        status, empty = self.client.request("GET", "/exchange-rates")
        self.assertEqual(status, 200)
        self.assertEqual(empty, [])
        today = date.today()
        save_exchange_rates(self.client.app.state.db_path, [
            ExchangeRate("USD", today - timedelta(days=1), Decimal("1400")),
            ExchangeRate("USD", today, Decimal("1410")),
            ExchangeRate("JPY", today - timedelta(days=1), Decimal("9.1")),
            ExchangeRate("EUR", today + timedelta(days=1), Decimal("1600")),
        ])
        status, rows = self.client.request("GET", "/exchange-rates")
        self.assertEqual(status, 200)
        by_currency = {row["currency"]: row for row in rows}
        self.assertEqual(set(by_currency), {"USD", "JPY"})
        self.assertEqual(by_currency["USD"]["rate_date"], today.isoformat())
        self.assertEqual(Decimal(by_currency["USD"]["krw_rate"]), Decimal("1410"))

    def test_exchange_rate_refresh_reuses_existing_collection_pipeline(self):
        observed = []

        def collect(db_path):
            observed.append(str(db_path))
            return save_exchange_rates(db_path, [
                ExchangeRate("CNY", date.today(), Decimal("200")),
            ])

        with patch("fx_tracker.main.sync_rates", side_effect=collect):
            status, rows = self.client.request("POST", "/exchange-rates/refresh")
        self.assertEqual(status, 200)
        self.assertEqual(len(observed), 1)
        self.assertEqual(rows[0]["currency"], "CNY")
        self.assertEqual(get_latest_exchange_rates(self.client.app.state.db_path)[0]["currency"], "CNY")


if __name__ == "__main__":
    unittest.main()
