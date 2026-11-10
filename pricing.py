"""Price conversion and change calculations for product history."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from .database import list_product_price_history, list_products, record_product_price


CENT = Decimal("0.01")


def calculate_krw_price(local_price: Decimal, exchange_rate: Decimal) -> Decimal:
    """Multiply a local price by its KRW rate and round the result to two decimals."""
    local_price = Decimal(str(local_price))
    exchange_rate = Decimal(str(exchange_rate))
    if not local_price.is_finite() or local_price < 0:
        raise ValueError("현지 가격은 0 이상의 유한한 숫자여야 합니다.")
    if not exchange_rate.is_finite() or exchange_rate <= 0:
        raise ValueError("환율은 0보다 큰 유한한 숫자여야 합니다.")
    # 곱셈은 Decimal로 끝까지 수행한 후 원화 표시용 소수 둘째 자리에서 반올림합니다.
    return (local_price * exchange_rate).quantize(CENT, rounding=ROUND_HALF_UP)


def calculate_percent_change(reference: Decimal | None, current: Decimal) -> Decimal | None:
    """Return percentage change; None indicates no usable positive reference price."""
    if reference is None:
        return None
    reference = Decimal(str(reference))
    current = Decimal(str(current))
    # 기준 금액이 없거나 0이면 분모가 될 수 없어 변동률을 계산하지 않습니다.
    if not reference.is_finite() or reference <= 0 or not current.is_finite():
        return None
    return (((current - reference) / reference) * Decimal("100")).quantize(
        CENT, rounding=ROUND_HALF_UP
    )


def refresh_product_price(db_path: str | Path, product_id: int, *, history_date: date | None = None):
    """Save one daily price snapshot using that currency's latest available rate."""
    # DB 저장 함수는 기록일보다 미래인 환율을 제외하고 가장 최근 환율을 선택합니다.
    return record_product_price(db_path, product_id, history_date=history_date or date.today())


def refresh_all_product_prices(db_path: str | Path, *, history_date: date | None = None) -> dict:
    """Update every product; products without a stored rate are reported as skipped."""
    snapshot_date = history_date or date.today()
    recorded = []
    already_recorded = []
    skipped = []
    for product in list_products(db_path):
        try:
            snapshot, created = record_product_price(db_path, product["id"], history_date=snapshot_date)
            (recorded if created else already_recorded).append(snapshot)
        except LookupError as exc:
            if str(exc) == "exchange_rate_not_found":
                skipped.append({
                    "product_id": product["id"],
                    "currency": product["currency"],
                    "reason": "저장된 환율 데이터가 없습니다.",
                })
            else:
                raise
    return {
        "history_date": snapshot_date,
        "recorded": recorded,
        "already_recorded": already_recorded,
        "skipped": skipped,
    }


def price_change_summary(db_path: str | Path, product_id: int) -> dict | None:
    """Compare the current snapshot with the first and immediately previous snapshots."""
    history = list_product_price_history(db_path, product_id)
    if not history:
        return None
    # 날짜순 첫 기록을 등록 기준으로, 마지막과 그 직전 기록을 비교합니다.
    current = history[-1]
    baseline = history[0]
    previous = history[-2] if len(history) > 1 else None
    current_price = Decimal(str(current["converted_price"]))
    baseline_price = Decimal(str(baseline["converted_price"]))
    previous_price = Decimal(str(previous["converted_price"])) if previous else None
    return {
        "product_id": product_id,
        "currency": current["currency"],
        "baseline_date": baseline["history_date"],
        "baseline_krw_price": baseline_price,
        "current_date": current["history_date"],
        "current_krw_price": current_price,
        "registration_change_percent": calculate_percent_change(baseline_price, current_price),
        "previous_date": previous["history_date"] if previous else None,
        "previous_krw_price": previous_price,
        "previous_change_percent": calculate_percent_change(previous_price, current_price),
    }
