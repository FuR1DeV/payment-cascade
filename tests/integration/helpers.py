import os
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://payments:payments@localhost:5432/payments_test"
)
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/15")


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": DATABASE_URL,
        "redis_url": REDIS_URL,
        "payment_time_budget_seconds": 5,
        "min_attempt_budget_seconds": 0.5,
        "status_check_initial_delay_seconds": 0.01,
        "status_check_max_delay_seconds": 0.05,
        "reconcile_backoff_base_seconds": 0,
        "reconcile_backoff_max_seconds": 0,
        "refund_backoff_base_seconds": 0,
        "circuit_min_requests": 4,
        "circuit_open_seconds": 30,
        "stale_payment_grace_seconds": 0,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def short_budget_settings() -> Settings:
    """Time budget runs out quickly: an unknown outcome ends in requires_review."""
    return make_settings(payment_time_budget_seconds=0.3, min_attempt_budget_seconds=0.1)


async def pay(
    client: httpx.AsyncClient, token: str = "tok_approve", key: str | None = None, **body: Any
) -> httpx.Response:
    payload = {
        "customer_id": "cust_1",
        "amount": 1999,
        "currency": "USD",
        "country": "DE",
        "payment_method": {"type": "card", "token": token},
    } | body
    return await client.post(
        "/payments", json=payload, headers={"Idempotency-Key": key or uuid4().hex}
    )


async def scalar(sql: str, **params: Any) -> Any:
    engine = create_async_engine(DATABASE_URL)
    async with engine.connect() as conn:
        value = (await conn.execute(text(sql), params)).scalar()
    await engine.dispose()
    return value
