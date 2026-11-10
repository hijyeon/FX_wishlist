import io
import json
import unittest
from datetime import date
from decimal import Decimal
from urllib.error import URLError

from fx_tracker.exchange_rates import ExchangeRateError, fetch_rate, fetch_rates, preprocess_rate


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


class ExchangeRateTests(unittest.TestCase):
    def test_preprocess_normalizes_codes_converts_decimal_and_preserves_api_date(self):
        rate = preprocess_rate({"date": "2026-10-16", "base": "jpy", "quote": "krw", "rate": "9.1234"})
        self.assertEqual(rate.currency, "JPY")
        self.assertEqual(rate.rate_date, date(2026, 10, 16))
        self.assertEqual(rate.krw_rate, Decimal("9.1234"))

    def test_fetch_uses_latest_rate_endpoint(self):
        seen = []

        def opener(request, timeout):
            seen.append(request.full_url)
            return FakeResponse({"date": "2026-10-19", "base": "USD", "quote": "KRW", "rate": 1400.2})

        rate = fetch_rate("usd", opener=opener)
        self.assertEqual(seen, ["https://api.frankfurter.dev/v2/rate/USD/KRW"])
        self.assertEqual(rate.krw_rate, Decimal("1400.2"))

    def test_rejects_missing_non_numeric_zero_negative_and_non_finite_rates(self):
        base = {"date": "2026-10-16", "base": "USD", "quote": "KRW"}
        for value in (None, "bad", 0, -1, "NaN", "Infinity"):
            with self.subTest(value=value), self.assertRaises(ExchangeRateError):
                preprocess_rate({**base, "rate": value})
        with self.assertRaises(ExchangeRateError):
            preprocess_rate(base)

    def test_rejects_wrong_pair_or_malformed_date(self):
        with self.assertRaises(ExchangeRateError):
            preprocess_rate({"date": "2026-10-16", "base": "USD", "quote": "EUR", "rate": 1})
        with self.assertRaises(ExchangeRateError):
            preprocess_rate({"date": "not-a-date", "base": "USD", "quote": "KRW", "rate": 1})

    def test_api_failure_is_reported_and_batch_does_not_continue_with_partial_data(self):
        calls = []

        def opener(request, timeout):
            calls.append(request.full_url)
            if len(calls) == 2:
                raise URLError("offline")
            return FakeResponse({"date": "2026-10-16", "base": "USD", "quote": "KRW", "rate": 1400})

        with self.assertRaises(ExchangeRateError):
            fetch_rates(("USD", "JPY"), opener=opener)
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
