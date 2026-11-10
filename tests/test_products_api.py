import asyncio
import json
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from fx_tracker.database import add_product, get_price_history, save_exchange_rates, save_price_history
from fx_tracker.exchange_rates import ExchangeRate
from fx_tracker.main import MAX_IMAGE_SIZE, create_app


def multipart(fields=None, file=None):
    boundary = "fx-tracker-boundary"
    chunks = []
    for key, value in (fields or {}).items():
        chunks.extend([
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n".encode(),
            str(value).encode(), b"\r\n",
        ])
    if file is not None:
        filename, mime, content = file
        chunks.extend([
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{filename}\"\r\nContent-Type: {mime}\r\n\r\n".encode(),
            content, b"\r\n",
        ])
    chunks.append(f"--{boundary}--\r\n".encode())
    return f"multipart/form-data; boundary={boundary}".encode(), b"".join(chunks)


class ASGIClient:
    """Tiny test client that drives the app directly, without external test packages."""

    def __init__(self, app):
        self.app = app

    def request(self, method, path, *, fields=None, file=None, content_type=None, body=None):
        if body is None and (fields is not None or file is not None or method in {"POST", "PUT"}):
            content_type, body = multipart(fields, file)
        body = body or b""
        headers = [(b"host", b"testserver")]
        if content_type:
            headers.append((b"content-type", content_type))
        messages = [{"type": "http.request", "body": body, "more_body": False}]
        response_messages = []

        async def receive():
            if messages:
                return messages.pop(0)
            return {"type": "http.disconnect"}

        async def send(message):
            response_messages.append(message)

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
            "query_string": b"", "root_path": "", "headers": headers,
            "server": ("testserver", 80), "client": ("testclient", 12345),
        }
        asyncio.run(self.app(scope, receive, send))
        start = next(message for message in response_messages if message["type"] == "http.response.start")
        content = b"".join(message.get("body", b"") for message in response_messages if message["type"] == "http.response.body")
        content_type = dict(start.get("headers", [])).get(b"content-type", b"")
        data = json.loads(content) if content and b"application/json" in content_type else (content or None)
        return start["status"], data


class ProductApiTests(unittest.TestCase):
    png = b"\x89PNG\r\n\x1a\n" + b"test-image-data"

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.upload_dir = root / "uploads"
        self.client = ASGIClient(create_app(root / "db.sqlite3", self.upload_dir))

    def tearDown(self):
        self.temp_dir.cleanup()

    def create_sample(self, *, image=True):
        return self.client.request(
            "POST", "/products",
            fields={"name": "Camera", "url": "https://shop.example/item", "currency": "USD", "original_price": "19.99"},
            file=("unsafe name.png", "image/png", self.png) if image else None,
        )

    def test_create_list_detail_and_partial_update(self):
        status_code, created = self.create_sample()
        self.assertEqual(status_code, 201)
        self.assertEqual(created["currency"], "USD")
        self.assertTrue(created["image_path"].startswith("uploads/"))
        self.assertEqual(len(list(self.upload_dir.iterdir())), 1)

        status_code, items = self.client.request("GET", "/products")
        self.assertEqual(status_code, 200)
        self.assertEqual(len(items), 1)
        status_code, detail = self.client.request("GET", f"/products/{created['id']}")
        self.assertEqual(status_code, 200)
        self.assertEqual(detail["name"], "Camera")

        status_code, updated = self.client.request("PUT", f"/products/{created['id']}", fields={"name": "New camera"})
        self.assertEqual(status_code, 200)
        self.assertEqual(updated["name"], "New camera")
        self.assertEqual(updated["currency"], "USD")
        self.assertEqual(updated["image_path"], created["image_path"])

    def test_validation_errors_for_price_currency_and_bad_image(self):
        for price in ("0", "-1", "not-a-price"):
            with self.subTest(price=price):
                result, body = self.client.request("POST", "/products", fields={"name": "Bad", "currency": "USD", "original_price": price})
                self.assertEqual(result, 422, body)
        result, _ = self.client.request("POST", "/products", fields={"name": "Bad", "currency": "KRW", "original_price": "1"})
        self.assertEqual(result, 422)
        result, _ = self.client.request(
            "POST", "/products", fields={"name": "Bad", "currency": "USD", "original_price": "1"},
            file=("x.png", "image/png", b"this is not png"),
        )
        self.assertEqual(result, 422)
        result, _ = self.client.request(
            "POST", "/products", fields={"name": "Bad", "currency": "USD", "original_price": "1"},
            file=("x.png", "image/png", b"\x89PNG\r\n\x1a\n" + b"x" * MAX_IMAGE_SIZE),
        )
        self.assertEqual(result, 413)

    def test_missing_ids_return_404(self):
        for method, path in (("GET", "/products/999"), ("PUT", "/products/999"), ("DELETE", "/products/999")):
            status_code, _ = self.client.request(method, path)
            self.assertEqual(status_code, 404)

    def test_delete_removes_image_and_cascades_price_history(self):
        status_code, created = self.create_sample()
        product_id = created["id"]
        rate_id = save_exchange_rates(self.client.app.state.db_path, [
            ExchangeRate("USD", date(2026, 10, 16), Decimal("1400"))
        ])[0]
        save_price_history(
            self.client.app.state.db_path, product_id=product_id, exchange_rate_id=rate_id,
            history_date=date(2026, 10, 16), price_used=Decimal("19.99"),
            exchange_rate_used=Decimal("1400"),
        )
        image = self.upload_dir / Path(created["image_path"]).name
        self.assertTrue(image.exists())
        status_code, _ = self.client.request("DELETE", f"/products/{product_id}")
        self.assertEqual(status_code, 204)
        self.assertFalse(image.exists())
        self.assertEqual(get_price_history(self.client.app.state.db_path, product_id), [])

    def test_swagger_openapi_declares_upload_and_crud_routes(self):
        schema = self.client.app.openapi()
        self.assertIn("/products", schema["paths"])
        self.assertIn("/products/{product_id}", schema["paths"])
        upload_schema = schema["paths"]["/products"]["post"]["requestBody"]["content"]["multipart/form-data"]["schema"]
        self.assertEqual(upload_schema["properties"]["image"]["format"], "binary")


if __name__ == "__main__":
    unittest.main()
