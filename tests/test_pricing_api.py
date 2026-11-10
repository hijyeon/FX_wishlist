import unittest
from datetime import date
from decimal import Decimal

from fx_tracker.database import add_product, save_exchange_rates, update_product
from fx_tracker.exchange_rates import ExchangeRate
from fx_tracker.pricing import calculate_krw_price, calculate_percent_change

from test_products_api import ASGIClient, create_app


class PricingApiTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path

        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.db_path = root / "pricing.sqlite3"
        self.client = ASGIClient(create_app(self.db_path, root / "uploads"))

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_product(self, currency="USD", price="10"):
        return add_product(
            self.db_path, name=f"{currency} item", currency=currency,
            original_price=Decimal(price),
        )

    def add_rate(self, currency, rate, rate_date):
        return save_exchange_rates(self.db_path, [
            ExchangeRate(currency, rate_date, Decimal(str(rate)))
        ])[0]

    def test_decimal_conversion_for_all_supported_currencies_and_actual_rate_date(self):
        examples = {
            "USD": ("1450.123456", "14501.23"),
            "JPY": ("9.123456", "91.23"),
            "CNY": ("200.56789", "2005.68"),
            "EUR": ("1700.555", "17005.55"),
        }
        today = date.today()
        for currency, (rate, expected) in examples.items():
            with self.subTest(currency=currency):
                product_id = self.make_product(currency)
                self.add_rate(currency, rate, today)
                status, body = self.client.request("POST", f"/products/{product_id}/price/refresh")
                self.assertEqual(status, 200)
                snapshot = body["snapshot"]
                self.assertEqual(snapshot["currency"], currency)
                self.assertEqual(snapshot["exchange_rate_date"], today.isoformat())
                self.assertEqual(Decimal(snapshot["converted_price"]), Decimal(expected))

    def test_missing_rate_returns_error_and_global_refresh_reports_skipped_item(self):
        product_id = self.make_product("EUR")
        status, body = self.client.request("POST", f"/products/{product_id}/price/refresh")
        self.assertEqual(status, 409)
        self.assertIn("환율", body["detail"])

        status, body = self.client.request("POST", "/prices/refresh")
        self.assertEqual(status, 200)
        self.assertEqual(body["recorded"], [])
        self.assertEqual(body["skipped"][0]["product_id"], product_id)

    def test_duplicate_daily_snapshot_is_idempotent(self):
        today = date.today()
        product_id = self.make_product("USD", "10")
        self.add_rate("USD", "1000", today)
        status, first = self.client.request("POST", f"/products/{product_id}/price/refresh")
        self.assertEqual(status, 200)
        update_product(self.db_path, product_id, original_price=Decimal("99"))
        status, second = self.client.request("POST", f"/products/{product_id}/price/refresh")
        self.assertEqual(status, 200)
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["snapshot"]["id"], second["snapshot"]["id"])
        self.assertEqual(Decimal(second["snapshot"]["converted_price"]), Decimal("10000.00"))

    def test_weekend_and_holiday_keep_history_date_separate_from_rate_date(self):
        product_id = self.make_product("JPY", "100")
        self.add_rate("JPY", "9.5", date(2025, 11, 7))  # Friday before the weekend.
        self.add_rate("JPY", "9.8", date(2025, 12, 24))  # Day before Christmas.
        # 과거 날짜를 지정해 저장 함수가 직전 영업일 환율을 선택하는지 확인합니다.
        from fx_tracker.pricing import refresh_product_price

        weekend_row, weekend_created = refresh_product_price(self.db_path, product_id, history_date=date(2025, 11, 8))
        holiday_row, holiday_created = refresh_product_price(self.db_path, product_id, history_date=date(2025, 12, 25))
        self.assertTrue(weekend_created)
        self.assertTrue(holiday_created)
        self.assertEqual(weekend_row["history_date"], "2025-11-08")
        self.assertEqual(weekend_row["exchange_rate_date"], "2025-11-07")
        self.assertEqual(holiday_row["history_date"], "2025-12-25")
        self.assertEqual(holiday_row["exchange_rate_date"], "2025-12-24")

    def test_price_change_rates_compare_first_and_previous_snapshots(self):
        product_id = self.make_product("USD", "10")
        dates_and_rates = [
            (date(2025, 11, 7), "1000"),
            (date(2025, 11, 8), "1100"),
            (date(2025, 11, 9), "990"),
        ]
        from fx_tracker.pricing import refresh_product_price

        for snapshot_date, rate in dates_and_rates:
            self.add_rate("USD", rate, snapshot_date)
            refresh_product_price(self.db_path, product_id, history_date=snapshot_date)
        status, body = self.client.request("GET", f"/products/{product_id}/price-changes")
        self.assertEqual(status, 200)
        self.assertEqual(Decimal(body["registration_change_percent"]), Decimal("-1.00"))
        self.assertEqual(Decimal(body["previous_change_percent"]), Decimal("-10.00"))
        status, history = self.client.request("GET", f"/products/{product_id}/price-history")
        self.assertEqual(status, 200)
        self.assertEqual([row["history_date"] for row in history], ["2025-11-07", "2025-11-08", "2025-11-09"])

    def test_missing_baseline_and_zero_reference_are_safe(self):
        product_id = self.make_product("CNY")
        status, body = self.client.request("GET", f"/products/{product_id}/price-changes")
        self.assertEqual(status, 200)
        self.assertIsNone(body["registration_change_percent"])
        self.assertIsNone(body["previous_change_percent"])
        self.assertIsNone(calculate_percent_change(Decimal("0"), Decimal("100")))
        self.assertIsNone(calculate_percent_change(None, Decimal("100")))
        self.assertEqual(calculate_krw_price(Decimal("1.005"), Decimal("1")), Decimal("1.01"))

    def test_global_refresh_records_available_rates_and_is_idempotent(self):
        product_id = self.make_product("EUR", "2")
        today = date.today()
        self.add_rate("EUR", "1500", today)
        status, first = self.client.request("POST", "/prices/refresh")
        self.assertEqual(status, 200)
        self.assertEqual(len(first["recorded"]), 1)
        self.assertEqual(first["recorded"][0]["product_id"], product_id)
        status, second = self.client.request("POST", "/prices/refresh")
        self.assertEqual(status, 200)
        self.assertEqual(second["recorded"], [])
        self.assertEqual(len(second["already_recorded"]), 1)

    def test_swagger_lists_pricing_endpoints(self):
        paths = self.client.app.openapi()["paths"]
        self.assertIn("/products/{product_id}/price", paths)
        self.assertIn("/products/{product_id}/price-history", paths)
        self.assertIn("/products/{product_id}/price-changes", paths)
        self.assertIn("/products/{product_id}/price/refresh", paths)
        self.assertIn("/prices/refresh", paths)


if __name__ == "__main__":
    unittest.main()
