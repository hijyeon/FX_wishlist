import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from urllib.error import URLError

from fx_tracker.database import get_exchange_rates
from fx_tracker.exchange_rates import ExchangeRateError
from fx_tracker.pipeline import sync_rates


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "pipeline.sqlite3"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_fetches_preprocesses_and_persists_batch(self):
        payloads = {
            "USD": {"date": "2026-10-16", "base": "USD", "quote": "KRW", "rate": 1400},
            "JPY": {"date": "2026-10-16", "base": "JPY", "quote": "KRW", "rate": 9.2},
        }
        ids = sync_rates(
            self.db_path,
            currencies=("USD", "JPY"),
            opener=lambda request, timeout: FakeResponse(payloads[request.full_url.rsplit("/", 2)[-2]]),
        )
        rows = get_exchange_rates(self.db_path)
        self.assertEqual(len(ids), 2)
        self.assertEqual([(r["currency"], r["rate_date"]) for r in rows],
                         [("JPY", "2026-10-16"), ("USD", "2026-10-16")])

    def test_api_failure_does_not_persist_a_partial_batch(self):
        calls = 0

        def opener(request, timeout):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise URLError("offline")
            return FakeResponse({"date": "2026-10-16", "base": "USD", "quote": "KRW", "rate": 1400})

        with self.assertRaises(ExchangeRateError):
            sync_rates(self.db_path, currencies=("USD", "JPY"), opener=opener)
        self.assertEqual(get_exchange_rates(self.db_path), [])


if __name__ == "__main__":
    unittest.main()
