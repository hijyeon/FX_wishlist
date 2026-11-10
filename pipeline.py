"""Orchestrate a complete Frankfurter fetch and SQLite save."""

from pathlib import Path
from typing import Callable, Any

from .database import initialize_database, save_exchange_rates
from .exchange_rates import SUPPORTED_CURRENCIES, fetch_rates


def sync_rates(
    db_path: str | Path,
    *,
    currencies: tuple[str, ...] = SUPPORTED_CURRENCIES,
    timeout: float = 15.0,
    opener: Callable[..., Any] | None = None,
) -> list[int]:
    """Fetch all requested rates first, then persist them as one database transaction."""
    initialize_database(db_path)
    kwargs = {"timeout": timeout}
    if opener is not None:
        kwargs["opener"] = opener
    rates = fetch_rates(currencies, **kwargs)
    return save_exchange_rates(db_path, rates)
