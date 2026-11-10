"""FastAPI CRUD API for products, reusing the project's SQLite database."""

from __future__ import annotations

import os
import uuid
from datetime import date
from decimal import Decimal
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .database import (
    add_product,
    delete_product as delete_product_row,
    get_latest_exchange_rates,
    get_product,
    initialize_database,
    list_products,
    list_product_price_history,
    update_product as update_product_row,
)
from .exchange_rates import ExchangeRateError
from .pipeline import sync_rates
from .pricing import price_change_summary, refresh_all_product_prices, refresh_product_price


Currency = Literal["USD", "JPY", "CNY", "EUR"]
MAX_IMAGE_SIZE = 5 * 1024 * 1024
MAX_REQUEST_SIZE = MAX_IMAGE_SIZE + 64 * 1024
IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp"}


class ProductCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    currency: Currency
    original_price: Annotated[Decimal, Field(gt=Decimal("0"), allow_inf_nan=False)]
    url: str | None = Field(default=None, max_length=2048)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError("상품 URL은 http 또는 https 주소여야 합니다.")
        return value


class ProductUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=200)
    currency: Currency | None = None
    original_price: Annotated[Decimal, Field(gt=Decimal("0"), allow_inf_nan=False)] | None = None
    url: str | None = Field(default=None, max_length=2048)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError("상품 URL은 http 또는 https 주소여야 합니다.")
        return value

    @model_validator(mode="after")
    def reject_null_required_fields(self):
        for field in ("name", "currency", "original_price"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} 값은 null일 수 없습니다.")
        return self


class ProductRead(BaseModel):
    id: int
    name: str
    url: str | None
    currency: str
    original_price: Decimal
    image_path: str | None
    created_at: str
    updated_at: str


class PriceHistoryRead(BaseModel):
    id: int
    product_id: int
    history_date: date
    exchange_rate_date: date
    currency: str
    price_used: Decimal
    exchange_rate_used: Decimal
    converted_price: Decimal


class PriceRefreshRead(BaseModel):
    snapshot: PriceHistoryRead
    created: bool


class PriceRefreshAllRead(BaseModel):
    history_date: date
    recorded: list[PriceHistoryRead]
    already_recorded: list[PriceHistoryRead]
    skipped: list[dict]


class PriceChangeRead(BaseModel):
    product_id: int
    currency: str | None
    baseline_date: date | None
    baseline_krw_price: Decimal | None
    current_date: date | None
    current_krw_price: Decimal | None
    registration_change_percent: Decimal | None
    previous_date: date | None
    previous_krw_price: Decimal | None
    previous_change_percent: Decimal | None


class ExchangeRateRead(BaseModel):
    currency: str
    rate_date: date
    krw_rate: Decimal
    source: str
    fetched_at: str


def _openapi_body(required: bool) -> dict:
    """Describe multipart fields so Swagger UI renders a local file chooser."""
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "maxLength": 200},
            "url": {"type": "string", "maxLength": 2048},
            "currency": {"type": "string", "enum": ["USD", "JPY", "CNY", "EUR"]},
            "original_price": {"type": "number", "exclusiveMinimum": 0},
            "image": {"type": "string", "format": "binary", "description": "최대 5MB, JPEG/PNG/GIF/WebP/BMP"},
        },
    }
    if required:
        schema["required"] = ["name", "currency", "original_price"]
    return {"requestBody": {"required": True, "content": {"multipart/form-data": {"schema": schema}}}}


async def _read_multipart(request: Request) -> tuple[dict[str, str], tuple[str, str, bytes] | None]:
    """Read a bounded multipart body using the standard library (no extra parser dependency)."""
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data") or "boundary=" not in content_type.lower():
        raise HTTPException(status_code=415, detail="요청은 multipart/form-data 형식이어야 합니다.")

    # 요청 전체 크기를 먼저 제한해 큰 본문이 메모리를 과도하게 쓰지 않게 합니다.
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_REQUEST_SIZE:
            raise HTTPException(status_code=413, detail="요청 크기가 허용 한도를 초과했습니다.")
        chunks.append(chunk)
    body = b"".join(chunks)
    envelope = (
        b"Content-Type: " + content_type.encode("latin-1") +
        b"\r\nMIME-Version: 1.0\r\n\r\n" + body
    )
    message = BytesParser(policy=policy.default).parsebytes(envelope)
    if not message.is_multipart():
        raise HTTPException(status_code=400, detail="multipart 요청 본문을 읽을 수 없습니다.")

    fields: dict[str, str] = {}
    image: tuple[str, str, bytes] | None = None
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename()
        content = part.get_payload(decode=True) or b""
        if name == "image":
            if image is not None:
                raise HTTPException(status_code=422, detail="이미지 파일은 한 개만 업로드할 수 있습니다.")
            if filename and content:
                if len(content) > MAX_IMAGE_SIZE:
                    raise HTTPException(status_code=413, detail="이미지 파일은 5MB 이하여야 합니다.")
                image = (filename, part.get_content_type().lower(), content)
        elif filename is None:
            try:
                fields[name] = content.decode(part.get_content_charset() or "utf-8")
            except (UnicodeDecodeError, LookupError) as exc:
                raise HTTPException(status_code=400, detail=f"{name} 필드의 문자 인코딩이 올바르지 않습니다.") from exc
    return fields, image


def _validate_image(upload: tuple[str, str, bytes]) -> str:
    """Detect supported image formats from their binary signatures, not filenames alone."""
    _filename, content_type, data = upload
    signatures = (
        (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
        (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
        (b"GIF87a", "image/gif", ".gif"),
        (b"GIF89a", "image/gif", ".gif"),
        (b"BM", "image/bmp", ".bmp"),
    )
    detected: tuple[str, str] | None = None
    for signature, mime, extension in signatures:
        if data.startswith(signature):
            detected = (mime, extension)
            break
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        detected = ("image/webp", ".webp")
    if detected is None or content_type not in IMAGE_TYPES or content_type != detected[0]:
        raise HTTPException(status_code=422, detail="지원하지 않는 이미지 형식입니다. JPEG, PNG, GIF, WebP, BMP만 가능합니다.")
    return detected[1]


def _save_image(upload_dir: Path, upload: tuple[str, str, bytes]) -> str:
    extension = _validate_image(upload)
    safe_name = f"{uuid.uuid4().hex}{extension}"
    upload_dir.mkdir(parents=True, exist_ok=True)
    (upload_dir / safe_name).write_bytes(upload[2])
    return f"uploads/{safe_name}"


def _remove_image(upload_dir: Path, stored_path: str | None) -> None:
    """Remove only a file directly inside this app's upload folder."""
    if not stored_path:
        return
    candidate = (upload_dir / Path(stored_path).name).resolve()
    if candidate.parent == upload_dir.resolve():
        candidate.unlink(missing_ok=True)


def _row_to_product(row) -> ProductRead:
    return ProductRead(
        id=row["id"], name=row["name"], url=row["url"], currency=row["currency"],
        original_price=row["original_price"], image_path=row["image_path"],
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


def _validation_error(exc: ValidationError) -> JSONResponse:
    details = [{"field": ".".join(map(str, error["loc"])), "message": error["msg"]} for error in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": details})


def create_app(
    db_path: str | Path | None = None,
    upload_dir: str | Path | None = None,
) -> FastAPI:
    """Build an app; paths can be overridden for tests or local development."""
    root = Path(__file__).resolve().parent
    database_path = Path(db_path or os.getenv("FX_TRACKER_DB", root / "data" / "fx_tracker.sqlite3"))
    images_path = Path(upload_dir or os.getenv("FX_TRACKER_UPLOAD_DIR", root / "uploads"))
    initialize_database(database_path)

    app = FastAPI(title="FX Tracker Product API", version="1.0.0")
    app.state.db_path = database_path
    app.state.upload_dir = images_path
    app.state.frontend_dir = root / "frontend"
    images_path.mkdir(parents=True, exist_ok=True)

    @app.get("/exchange-rates", response_model=list[ExchangeRateRead], summary="통화별 최신 저장 환율 조회")
    async def read_exchange_rates():
        return [dict(row) for row in get_latest_exchange_rates(app.state.db_path)]

    @app.post("/exchange-rates/refresh", response_model=list[ExchangeRateRead], summary="최신 환율 수집 및 저장")
    def refresh_exchange_rates():
        try:
            # 기존 수집 파이프라인을 호출해 검증된 환율만 DB에 기록합니다.
            sync_rates(app.state.db_path)
        except ExchangeRateError as exc:
            raise HTTPException(status_code=502, detail=f"환율 API에서 데이터를 가져오지 못했습니다: {exc}") from exc
        return [dict(row) for row in get_latest_exchange_rates(app.state.db_path)]

    @app.post("/products", response_model=ProductRead, status_code=status.HTTP_201_CREATED,
              openapi_extra=_openapi_body(required=True), summary="상품 등록")
    async def create_product(request: Request):
        fields, image = await _read_multipart(request)
        # 입력값을 검증한 뒤 이미지 파일과 상품 정보를 저장합니다.
        try:
            product = ProductCreate.model_validate(fields)
        except ValidationError as exc:
            return _validation_error(exc)
        image_path = _save_image(app.state.upload_dir, image) if image else None
        try:
            product_id = add_product(
                app.state.db_path, name=product.name, url=product.url,
                currency=product.currency, original_price=product.original_price,
                image_path=image_path,
            )
        except Exception:
            _remove_image(app.state.upload_dir, image_path)
            raise
        # 상품 등록과 함께 저장된 최신 환율로 오늘 가격 이력을 바로 만듭니다.
        # 해당 통화 환율이 아직 없으면 상품 등록은 유지하고, 임의의 가격은 만들지 않습니다.
        try:
            refresh_product_price(app.state.db_path, product_id)
        except LookupError as exc:
            if str(exc) != "exchange_rate_not_found":
                raise
        return _row_to_product(get_product(app.state.db_path, product_id))

    @app.get("/products", response_model=list[ProductRead], summary="전체 상품 조회")
    async def read_products():
        return [_row_to_product(row) for row in list_products(app.state.db_path)]

    @app.get("/products/{product_id}", response_model=ProductRead, summary="상품 상세 조회")
    async def read_product(product_id: int):
        row = get_product(app.state.db_path, product_id)
        if row is None:
            raise HTTPException(status_code=404, detail="상품을 찾을 수 없습니다.")
        return _row_to_product(row)

    @app.post("/products/{product_id}/price/refresh", response_model=PriceRefreshRead,
              summary="상품 원화 가격 계산 및 오늘 이력 저장")
    async def refresh_one_price(product_id: int):
        try:
            snapshot, created = refresh_product_price(app.state.db_path, product_id)
        except LookupError as exc:
            if str(exc) == "product_not_found":
                raise HTTPException(status_code=404, detail="상품을 찾을 수 없습니다.") from exc
            raise HTTPException(
                status_code=409,
                detail="해당 통화의 저장된 환율이 없습니다. 먼저 환율을 수집해 주세요.",
            ) from exc
        return PriceRefreshRead(snapshot=dict(snapshot), created=created)

    @app.post("/prices/refresh", response_model=PriceRefreshAllRead,
              summary="전체 상품의 오늘 원화 가격 이력 갱신")
    async def refresh_all_prices():
        result = refresh_all_product_prices(app.state.db_path)
        return PriceRefreshAllRead(
            history_date=result["history_date"],
            recorded=[dict(row) for row in result["recorded"]],
            already_recorded=[dict(row) for row in result["already_recorded"]],
            skipped=result["skipped"],
        )

    @app.get("/products/{product_id}/price", response_model=PriceHistoryRead,
             summary="상품의 최신 원화 환산 가격 조회")
    async def read_latest_price(product_id: int):
        if get_product(app.state.db_path, product_id) is None:
            raise HTTPException(status_code=404, detail="상품을 찾을 수 없습니다.")
        history = list_product_price_history(app.state.db_path, product_id)
        if not history:
            raise HTTPException(status_code=404, detail="가격 이력이 없습니다. 먼저 가격 갱신을 실행해 주세요.")
        return dict(history[-1])

    @app.get("/products/{product_id}/price-history", response_model=list[PriceHistoryRead],
             summary="상품의 날짜순 원화 가격 이력 조회")
    async def read_price_history(product_id: int):
        if get_product(app.state.db_path, product_id) is None:
            raise HTTPException(status_code=404, detail="상품을 찾을 수 없습니다.")
        return [dict(row) for row in list_product_price_history(app.state.db_path, product_id)]

    @app.get("/products/{product_id}/price-changes", response_model=PriceChangeRead,
             summary="등록 시점 및 직전 기록 대비 변동률 조회")
    async def read_price_changes(product_id: int):
        product = get_product(app.state.db_path, product_id)
        if product is None:
            raise HTTPException(status_code=404, detail="상품을 찾을 수 없습니다.")
        summary = price_change_summary(app.state.db_path, product_id)
        if summary is None:
            summary = {
                "product_id": product_id, "currency": product["currency"],
                "baseline_date": None, "baseline_krw_price": None,
                "current_date": None, "current_krw_price": None,
                "registration_change_percent": None, "previous_date": None,
                "previous_krw_price": None, "previous_change_percent": None,
            }
        return summary

    @app.put("/products/{product_id}", response_model=ProductRead,
             openapi_extra=_openapi_body(required=False), summary="상품 수정")
    async def update_product(product_id: int, request: Request):
        existing = get_product(app.state.db_path, product_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="상품을 찾을 수 없습니다.")
        fields, image = await _read_multipart(request)
        # 전달된 필드만 반영하므로 요청에서 빠진 기존 값은 그대로 유지됩니다.
        try:
            product = ProductUpdate.model_validate(fields)
        except ValidationError as exc:
            return _validation_error(exc)
        changes = product.model_dump(exclude_unset=True)
        if not changes and image is None:
            raise HTTPException(status_code=422, detail="수정할 항목을 하나 이상 입력해 주세요.")
        # 새 파일을 저장하고 DB 수정이 끝난 뒤 기존 이미지 파일을 정리합니다.
        new_image_path = _save_image(app.state.upload_dir, image) if image else None
        if new_image_path:
            changes["image_path"] = new_image_path
        try:
            updated = update_product_row(app.state.db_path, product_id, **changes)
        except Exception:
            _remove_image(app.state.upload_dir, new_image_path)
            raise
        if not updated:
            _remove_image(app.state.upload_dir, new_image_path)
            raise HTTPException(status_code=404, detail="상품을 찾을 수 없습니다.")
        if new_image_path:
            _remove_image(app.state.upload_dir, existing["image_path"])
        return _row_to_product(get_product(app.state.db_path, product_id))

    @app.delete("/products/{product_id}", status_code=status.HTTP_204_NO_CONTENT, summary="상품 삭제")
    async def delete_product(product_id: int):
        image_path = delete_product_row(app.state.db_path, product_id)
        if image_path is None:
            raise HTTPException(status_code=404, detail="상품을 찾을 수 없습니다.")
        # 기존 외래 키의 ON DELETE CASCADE가 가격 이력을 함께 지웁니다.
        _remove_image(app.state.upload_dir, image_path)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # API 라우트를 먼저 둔 뒤 프론트엔드와 업로드 이미지를 정적으로 제공합니다.
    app.mount("/uploads", StaticFiles(directory=images_path), name="uploads")
    app.mount("/", StaticFiles(directory=app.state.frontend_dir, html=True), name="frontend")

    return app


app = create_app()
