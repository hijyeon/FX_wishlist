# FX tracker data layer

This MVP contains Frankfurter exchange-rate collection/preprocessing, SQLite persistence, and a FastAPI product CRUD API. SQLite access continues to use the standard library. FastAPI and Pydantic handle the HTTP API and request validation.

## Requirements

- Python 3.10 or newer
- Internet access when collecting live rates

Install the API dependencies with `python -m pip install -r fx_tracker/requirements.txt` from the `Documents/Codex` directory. The exchange-rate client calls Frankfurter v2 `/rate/{base}/KRW` for USD, JPY, CNY, and EUR. The API's `date` is kept as the rate date, including when it is an earlier business day.

## Product API

From the `Documents/Codex` directory, start the API:

```powershell
python -m uvicorn fx_tracker.main:app --reload
```

Open `http://127.0.0.1:8000/docs` to use Swagger UI. The endpoints are `POST /products`, `GET /products`, `GET /products/{product_id}`, `PUT /products/{product_id}`, and `DELETE /products/{product_id}`. Create and update use `multipart/form-data`; in Swagger, choose an image file in the `image` field. Supported image files are JPEG, PNG, GIF, WebP, and BMP up to 5 MB. File type is checked against the binary signature, and disk names are random UUIDs. The database stores a relative path such as `uploads/<random-name>.png`.

The default SQLite file is `fx_tracker/data/fx_tracker.sqlite3` and images are written to `fx_tracker/uploads/`. Set `FX_TRACKER_DB` or `FX_TRACKER_UPLOAD_DIR` to override either location. Partial updates keep omitted fields and the current image. Replacing an image removes the old uploaded file after the database update. Deleting a product cascades its price-history rows using the existing foreign key and removes its uploaded image.

## FX Wishlist website

The HTML/CSS/JavaScript frontend is served by the same local FastAPI server. Run `python -m uvicorn fx_tracker.main:app --reload` from `Documents/Codex`, then open `http://127.0.0.1:8000/`. `/docs` remains available for API testing. The first page load reads `/exchange-rates` and `/products`; use **환율·가격 갱신** to call `POST /exchange-rates/refresh`, then `POST /prices/refresh`. This stores live API rates and creates price-history records where rates exist. Chart.js is loaded from jsDelivr, so the chart needs an internet connection; the rest of the frontend uses local files and the same-origin API.

Files are separated by role: `frontend/index.html` provides the page shell, `frontend/styles.css` defines the responsive visual design, and `frontend/app.js` handles navigation, forms, API requests, and Chart.js. Data is loaded with `fetch`, then rendered into the wishlist, detail page, and price chart. Product images are served from `/uploads/{filename}`.

## KRW price history API

- `POST /products/{product_id}/price/refresh`: calculate one product's price and store today's snapshot. Returns `409` when no eligible exchange rate is stored.
- `POST /prices/refresh`: refresh all products. Products without an eligible rate are listed under `skipped`; no placeholder rate is used.
- `GET /products/{product_id}/price`: read the latest saved KRW price.
- `GET /products/{product_id}/price-history`: read snapshots in date order.
- `GET /products/{product_id}/price-changes`: compare the first snapshot with the latest, and the previous snapshot with the latest. If a baseline is missing or zero, the corresponding percentage is `null`.

The calculation uses the most recent `exchange_rates.rate_date` on or before the snapshot date. `price_history.history_date` is the day the snapshot is recorded; `exchange_rate_date` in responses is joined from the referenced exchange-rate row. The existing `exchange_rate_id` foreign key already preserves this relationship, and the unique `(product_id, history_date)` constraint makes refreshes idempotent for each day, so no schema migration was required.

## Initialize and collect

From the `Documents/Codex` directory:

```powershell
python -c "from fx_tracker.pipeline import sync_rates; print(sync_rates('fx_tracker/data/fx_tracker.sqlite3'))"
```

The function fetches and validates the complete batch before saving. A failed request or invalid/missing rate aborts the batch; it does not store a placeholder or a partial result. Repeated collection for an already stored currency/date leaves the original observation intact and returns its existing row ID.

## Read stored rates

```powershell
python -c "from fx_tracker.database import get_exchange_rates; print([dict(r) for r in get_exchange_rates('fx_tracker/data/fx_tracker.sqlite3')])"
```

Individual functions are available in `fx_tracker.exchange_rates` (`fetch_rate`, `fetch_rates`, `preprocess_rate`) and `fx_tracker.database` (`initialize_database`, `save_exchange_rates`, `get_exchange_rates`, `add_product`, `save_price_history`, `get_price_history`).

## Run tests

From the `Documents/Codex` directory:

```powershell
python -m unittest discover -s fx_tracker/tests -v
```

Tests use a mocked HTTP opener, a small in-process ASGI test client, and temporary SQLite databases; they do not call the live API or create the default project database.
