"""Fetch and validate KRW exchange rates from Frankfurter's v2 API."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


API_BASE_URL = "https://api.frankfurter.dev/v2"
SUPPORTED_CURRENCIES = ("USD", "JPY", "CNY", "EUR")
SOURCE = "Frankfurter API"


class ExchangeRateError(RuntimeError):
    """Raised when an API request or exchange-rate payload is unusable."""


@dataclass(frozen=True)
class ExchangeRate:
    currency: str
    rate_date: date
    krw_rate: Decimal
    source: str = SOURCE


def preprocess_rate(payload: Any, expected_currency: str | None = None) -> ExchangeRate:
    """Validate and normalize one Frankfurter v2 /rate/{base}/KRW response."""
    if not isinstance(payload, dict):
        raise ExchangeRateError("Frankfurter response must be a JSON object")
    try:
        currency = str(payload["base"]).strip().upper()
        quote = str(payload["quote"]).strip().upper()
        rate_date = date.fromisoformat(str(payload["date"]))
        raw_rate = payload["rate"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ExchangeRateError("Response is missing a valid base, quote, date, or rate") from exc

    if expected_currency and currency != expected_currency.strip().upper():
        raise ExchangeRateError(f"Expected base {expected_currency.upper()}, received {currency}")
    if currency not in SUPPORTED_CURRENCIES:
        raise ExchangeRateError(f"Unsupported currency: {currency}")
    if quote != "KRW":
        raise ExchangeRateError(f"Expected quote KRW, received {quote}")
    try:
        rate = Decimal(str(raw_rate))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ExchangeRateError("Rate must be numeric") from exc
    if not rate.is_finite() or rate <= 0:
        raise ExchangeRateError("Rate must be a finite number greater than zero")

    return ExchangeRate(currency=currency, rate_date=rate_date, krw_rate=rate)


def fetch_rate(
    currency: str,
    *,
    timeout: float = 15.0,
    opener: Callable[..., Any] = urlopen,
) -> ExchangeRate:
    """Fetch a single currency's latest rate against KRW and preprocess it."""
    normalized = currency.strip().upper()
    if normalized not in SUPPORTED_CURRENCIES:
        raise ExchangeRateError(f"Unsupported currency: {normalized}")
    url = f"{API_BASE_URL}/rate/{normalized}/KRW"
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "fx-tracker/1.0"})
    try:
        with opener(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise ExchangeRateError(f"Frankfurter returned HTTP {exc.code} for {normalized}/KRW") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ExchangeRateError(f"Frankfurter request failed for {normalized}/KRW: {exc}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExchangeRateError(f"Frankfurter returned invalid JSON for {normalized}/KRW") from exc
    return preprocess_rate(payload, expected_currency=normalized)


def fetch_rates(
    currencies: tuple[str, ...] = SUPPORTED_CURRENCIES,
    *,
    timeout: float = 15.0,
    opener: Callable[..., Any] = urlopen,
) -> list[ExchangeRate]:
    """Fetch a complete batch; errors abort the batch so callers cannot save partial data."""
    return [fetch_rate(code, timeout=timeout, opener=opener) for code in currencies]
