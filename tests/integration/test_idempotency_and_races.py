"""Idempotency and races on a real PostgreSQL: several requests hit the database at once."""

import asyncio
from collections import Counter
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.main import create_app
from app.modules.payments.bootstrap import Container, build_container
from tests.fakes import FakeProvider, RecordingJobs, signed_webhook, tech, unknown

from .helpers import DATABASE_URL, pay, scalar, short_budget_settings


async def test_concurrent_double_submit_creates_one_payment_and_one_charge(
    client: httpx.AsyncClient, alpha: FakeProvider
) -> None:
    alpha.delay = 0.3  # keep the first request in flight while the others arrive
    key = uuid4().hex
    responses = await asyncio.gather(*(pay(client, key=key) for _ in range(5)))
    codes = Counter(r.status_code for r in responses)
    assert codes[201] == 1
    assert codes[201] + codes[409] + codes[200] == 5
    assert await scalar("SELECT count(*) FROM payments") == 1
    assert len(alpha.charge_calls) == 1


async def test_replay_returns_the_same_result(
    client: httpx.AsyncClient, alpha: FakeProvider
) -> None:
    key = uuid4().hex
    first = await pay(client, key=key)
    second = await pay(client, key=key)
    assert (first.status_code, second.status_code) == (201, 200)
    assert second.headers["Idempotent-Replayed"] == "true"
    assert second.json() == first.json()
    assert len(alpha.charge_calls) == 1


async def test_same_key_with_different_body_is_rejected(client: httpx.AsyncClient) -> None:
    key = uuid4().hex
    assert (await pay(client, key=key, amount=1000)).status_code == 201
    reused = await pay(client, key=key, amount=5000)
    assert reused.status_code == 422
    assert reused.json()["detail"]["code"] == "idempotency_key_reused"


async def test_key_is_scoped_by_customer(client: httpx.AsyncClient) -> None:
    key = uuid4().hex
    a = await pay(client, key=key, customer_id="cust_a")
    b = await pay(client, key=key, customer_id="cust_b")
    assert a.status_code == b.status_code == 201
    assert a.json()["id"] != b.json()["id"]


@pytest.fixture
async def review_container(
    alpha: FakeProvider, beta: FakeProvider, gamma: FakeProvider, jobs: RecordingJobs
):  # type: ignore[no-untyped-def]
    c = await build_container(short_budget_settings(), providers=[alpha, beta, gamma], jobs=jobs)
    yield c
    await c.close()


async def test_concurrent_webhooks_advance_state_once(
    review_container: Container, alpha: FakeProvider
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(review_container)), base_url="http://test"
    ) as client:
        alpha.charge_script = [unknown("timeout")]
        payment = (await pay(client)).json()
        assert payment["status"] == "requires_review"

        def webhook(event_id: str) -> tuple[bytes, dict[str, str]]:
            return signed_webhook(
                event_id=event_id,
                attempt_key=f"{payment['id']}:1",
                provider_payment_id="alpha_1",
                kind="approved",
            )

        # Same event delivered three times plus two different events with the same news.
        events = [
            webhook("evt_a"),
            webhook("evt_a"),
            webhook("evt_a"),
            webhook("evt_b"),
            webhook("evt_c"),
        ]
        responses = await asyncio.gather(
            *(client.post("/webhooks/mock_alpha", content=b, headers=h) for b, h in events)
        )
        results = Counter(r.json()["result"] for r in responses)
        assert results == {"applied": 1, "duplicate": 2, "no_change": 2}

        final = (await client.get(f"/payments/{payment['id']}")).json()
        assert final["status"] == "succeeded"
        assert await scalar("SELECT count(*) FROM webhook_events") == 3
        assert await scalar("SELECT count(*) FROM payment_alerts") == 0


async def test_concurrent_late_approvals_refund_once(
    client: httpx.AsyncClient, alpha: FakeProvider, jobs: RecordingJobs
) -> None:
    alpha.charge_script = [tech("96")]
    payment = (await pay(client)).json()
    assert payment["succeeded_provider"] == "mock_beta"
    events = [
        signed_webhook(event_id=f"evt_{i}", attempt_key=f"{payment['id']}:1", kind="approved")
        for i in range(4)
    ]
    responses = await asyncio.gather(
        *(client.post("/webhooks/mock_alpha", content=b, headers=h) for b, h in events)
    )
    assert Counter(r.json()["result"] for r in responses) == {"applied": 1, "no_change": 3}
    assert await scalar("SELECT count(*) FROM payment_alerts WHERE kind = 'extra_charge'") == 1
    assert jobs.refund_kicks == 1


async def test_webhook_with_bad_signature_is_rejected_and_not_stored(
    client: httpx.AsyncClient,
) -> None:
    body, headers = signed_webhook(event_id="evt_x", attempt_key="x:1", kind="approved")
    headers["X-Fake-Signature"] = "0" * 64
    response = await client.post("/webhooks/mock_alpha", content=body, headers=headers)
    assert response.status_code == 400
    assert (await client.post("/webhooks/nope", content=body, headers=headers)).status_code == 404
    assert await scalar("SELECT count(*) FROM webhook_events") == 0


async def test_webhook_from_other_provider_cannot_settle_an_attempt(
    client: httpx.AsyncClient, alpha: FakeProvider
) -> None:
    alpha.charge_script = [unknown("timeout")]
    alpha.status_script = [tech("not_found")]
    payment = (await pay(client)).json()  # attempt 1 at alpha (error), 2 at beta (approved)
    body, headers = signed_webhook(event_id="e", attempt_key=f"{payment['id']}:1", kind="approved")
    response = await client.post("/webhooks/mock_gamma", content=body, headers=headers)
    assert response.json() == {"result": "unknown_payment"}


async def test_webhook_journal_is_append_only(client: httpx.AsyncClient) -> None:
    body, headers = signed_webhook(event_id="evt_1", attempt_key="none:1", kind="approved")
    await client.post("/webhooks/mock_alpha", content=body, headers=headers)
    engine = create_async_engine(DATABASE_URL)
    try:
        async with engine.begin() as conn:
            with pytest.raises(DBAPIError, match="append-only"):
                await conn.execute(text("UPDATE webhook_events SET event_type = 'x'"))
    finally:
        await engine.dispose()


async def test_database_forbids_two_in_flight_attempts() -> None:
    """Even if application code had a bug, a second unresolved charge cannot be recorded."""
    engine = create_async_engine(DATABASE_URL)
    pid, now = uuid4(), datetime.now(UTC)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO payments (id, customer_id, idempotency_key, request_hash,"
                    " amount_minor, currency, country, method, payment_token, status, route,"
                    " created_at, updated_at, deadline_at) VALUES (:id, 'c', 'k', 'h', 100, 'USD',"
                    " 'DE', 'card', 'tok_x', 'processing', '{a,b}', :now, :now, :now)"
                ),
                {"id": pid, "now": now},
            )
            insert = text(
                "INSERT INTO payment_attempts (id, payment_id, attempt_no, provider,"
                " idempotency_key, status, created_at, updated_at)"
                " VALUES (:id, :pid, :no, :provider, :key, :status, :now, :now)"
            )
            await conn.execute(
                insert,
                {
                    "id": uuid4(),
                    "pid": pid,
                    "no": 1,
                    "provider": "a",
                    "key": f"{pid}:1",
                    "status": "unknown",
                    "now": now,
                },
            )
            with pytest.raises(IntegrityError, match="uq_payment_attempts_one_in_flight"):
                await conn.execute(
                    insert,
                    {
                        "id": uuid4(),
                        "pid": pid,
                        "no": 2,
                        "provider": "b",
                        "key": f"{pid}:2",
                        "status": "pending",
                        "now": now,
                    },
                )
    finally:
        await engine.dispose()
