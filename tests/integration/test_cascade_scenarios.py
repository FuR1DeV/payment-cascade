"""End-to-end cascade scenarios through the HTTP API, on real PostgreSQL and Redis."""

import httpx

from app.main import create_app
from app.modules.payments.bootstrap import Container, build_container
from tests.fakes import (
    FakeProvider,
    RecordingJobs,
    approved,
    hard,
    signed_webhook,
    soft,
    tech,
    unknown,
)

from .helpers import pay, scalar, short_budget_settings


def statuses(payment: dict) -> list[tuple[str, str]]:
    return [(a["provider"], a["status"]) for a in payment["attempts"]]


async def test_soft_decline_cascades_to_next_provider(
    client: httpx.AsyncClient, alpha: FakeProvider, beta: FakeProvider
) -> None:
    alpha.charge_script = [soft("05")]
    response = await pay(client)
    assert response.status_code == 201
    payment = response.json()
    assert payment["status"] == "succeeded"
    assert payment["succeeded_provider"] == "mock_beta"
    assert statuses(payment) == [("mock_alpha", "declined_soft"), ("mock_beta", "approved")]
    # Every attempt has its own stable idempotency key.
    assert [c.idempotency_key for c in alpha.charge_calls + beta.charge_calls] == [
        f"{payment['id']}:1",
        f"{payment['id']}:2",
    ]


async def test_hard_decline_stops_the_cascade(
    client: httpx.AsyncClient, alpha: FakeProvider, beta: FakeProvider
) -> None:
    alpha.charge_script = [hard("stolen_card")]
    payment = (await pay(client, "tok_hard_decline")).json()
    assert payment["status"] == "failed"
    assert payment["failure_reason"] == "hard_decline:mock_alpha:stolen_card"
    assert beta.charge_calls == []


async def test_technical_error_cascades(
    client: httpx.AsyncClient, alpha: FakeProvider, beta: FakeProvider
) -> None:
    alpha.charge_script = [tech("connect_failed")]
    payment = (await pay(client)).json()
    assert statuses(payment) == [("mock_alpha", "error"), ("mock_beta", "approved")]


async def test_timeout_and_provider_charged_means_no_cascade(
    client: httpx.AsyncClient, alpha: FakeProvider, beta: FakeProvider
) -> None:
    alpha.charge_script = [unknown("timeout")]
    alpha.status_script = [unknown("processing"), approved("alpha_77")]
    payment = (await pay(client)).json()
    assert payment["status"] == "succeeded"
    assert statuses(payment) == [("mock_alpha", "approved")]
    assert payment["attempts"][0]["provider_payment_id"] == "alpha_77"
    assert len(alpha.status_calls) == 2
    assert payment["attempts"][0]["status_checks"] == 2
    assert beta.charge_calls == []  # the whole point: no second charge


async def test_timeout_and_provider_did_not_process_cascades(
    client: httpx.AsyncClient, alpha: FakeProvider, beta: FakeProvider
) -> None:
    alpha.charge_script = [unknown("timeout")]
    alpha.status_script = [tech("not_found")]
    payment = (await pay(client)).json()
    assert statuses(payment) == [("mock_alpha", "error"), ("mock_beta", "approved")]
    assert [a["status_checks"] for a in payment["attempts"]] == [1, 0]


async def test_unresolved_timeout_goes_to_review_then_reconciliation_settles_it(
    alpha: FakeProvider, beta: FakeProvider, gamma: FakeProvider, jobs: RecordingJobs
) -> None:
    settings = short_budget_settings()
    container = await build_container(settings, providers=[alpha, beta, gamma], jobs=jobs)
    try:
        app = create_app(container)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            alpha.charge_script = [unknown("timeout")]
            payment = (await pay(client)).json()
            assert payment["status"] == "requires_review"
            assert statuses(payment) == [("mock_alpha", "unknown")]
            assert beta.charge_calls == []

            # Background task: provider still does not know.
            assert await container.reconcile_unknown() == 1
            still = (await client.get(f"/payments/{payment['id']}")).json()
            assert still["status"] == "requires_review"

            # Later the provider reports the charge.
            alpha.status_script = [approved("alpha_9")]
            await container.reconcile_unknown()
            settled = (await client.get(f"/payments/{payment['id']}")).json()
            assert settled["status"] == "succeeded"
            assert settled["succeeded_provider"] == "mock_alpha"
            assert beta.charge_calls == []
    finally:
        await container.close()


async def test_review_resolves_to_failed_when_provider_never_got_it(
    alpha: FakeProvider, beta: FakeProvider, gamma: FakeProvider, jobs: RecordingJobs
) -> None:
    container = await build_container(
        short_budget_settings(), providers=[alpha, beta, gamma], jobs=jobs
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(container)), base_url="http://test"
        ) as client:
            alpha.charge_script = [unknown("timeout")]
            payment = (await pay(client)).json()
            alpha.status_script = [tech("not_found")]
            await container.reconcile_unknown()
            final = (await client.get(f"/payments/{payment['id']}")).json()
            assert final["status"] == "failed"
            assert final["failure_reason"] == "not_charged_after_review"
    finally:
        await container.close()


async def test_review_resolved_by_a_decline_webhook(
    alpha: FakeProvider, beta: FakeProvider, gamma: FakeProvider, jobs: RecordingJobs
) -> None:
    container = await build_container(
        short_budget_settings(), providers=[alpha, beta, gamma], jobs=jobs
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(container)), base_url="http://test"
        ) as client:
            alpha.charge_script = [unknown("timeout")]
            payment = (await pay(client)).json()
            assert payment["status"] == "requires_review"
            body, headers = signed_webhook(
                event_id="evt_d", attempt_key=f"{payment['id']}:1", kind="soft_decline", code="05"
            )
            await client.post("/webhooks/mock_alpha", content=body, headers=headers)
            final = (await client.get(f"/payments/{payment['id']}")).json()
            assert final["status"] == "failed"
            assert final["failure_reason"] == "not_charged_after_review"
            assert await container.reconcile_unknown() == 0
    finally:
        await container.close()


async def test_late_webhook_after_cascade_is_detected_and_refunded(
    client: httpx.AsyncClient,
    container: Container,
    alpha: FakeProvider,
    beta: FakeProvider,
    jobs: RecordingJobs,
) -> None:
    alpha.charge_script = [tech("96")]  # alpha says "not processed"...
    payment = (await pay(client, "tok_late_webhook")).json()
    assert payment["succeeded_provider"] == "mock_beta"

    # ...but charges anyway and tells us in a webhook after we already took money at beta.
    body, headers = signed_webhook(
        event_id="evt_late_1",
        attempt_key=f"{payment['id']}:1",
        provider_payment_id="alpha_late",
        kind="approved",
    )
    response = await client.post("/webhooks/mock_alpha", content=body, headers=headers)
    assert response.json() == {"result": "applied"}

    after = (await client.get(f"/payments/{payment['id']}")).json()
    assert after["status"] == "succeeded"
    assert after["succeeded_provider"] == "mock_beta"
    assert statuses(after) == [("mock_alpha", "refund_pending"), ("mock_beta", "approved")]
    assert after["attempts"][0]["late_approval"] is True

    alerts = (await client.get("/alerts", params={"payment_id": payment["id"]})).json()
    assert [a["kind"] for a in alerts] == ["extra_charge"]
    assert jobs.refund_kicks == 1

    # The refund task does the refund, with an idempotency key derived from the attempt.
    assert await container.execute_refunds() == 1
    assert [r.idempotency_key for r in alpha.refund_calls] == [f"refund:{payment['id']}:1"]
    refunded = (await client.get(f"/payments/{payment['id']}")).json()
    assert refunded["attempts"][0]["status"] == "refunded"
    assert await container.execute_refunds() == 0
    assert len(alpha.refund_calls) == 1


async def test_failed_refund_is_retried_and_alerted(
    client: httpx.AsyncClient, container: Container, alpha: FakeProvider
) -> None:
    alpha.charge_script = [tech("96")]
    alpha.refund_ok = False
    payment = (await pay(client)).json()
    body, headers = signed_webhook(
        event_id="evt_1", attempt_key=f"{payment['id']}:1", kind="approved"
    )
    await client.post("/webhooks/mock_alpha", content=body, headers=headers)
    for _ in range(container.settings.refund_alert_after_attempts):
        await container.execute_refunds()
    alerts = (await client.get("/alerts", params={"payment_id": payment["id"]})).json()
    assert sorted(a["kind"] for a in alerts) == ["extra_charge", "refund_failed"]


async def test_all_soft_declines_fail_after_max_attempts(
    client: httpx.AsyncClient, alpha, beta, gamma
) -> None:  # type: ignore[no-untyped-def]
    for provider in (alpha, beta, gamma):
        provider.charge_script = [soft()]
    payment = (await pay(client, "tok_all_soft")).json()
    assert payment["status"] == "failed"
    assert payment["failure_reason"] == "max_attempts_reached"
    assert len(payment["attempts"]) == 3


async def test_open_circuit_skips_provider(
    client: httpx.AsyncClient, container: Container, alpha: FakeProvider
) -> None:
    for _ in range(container.settings.circuit_min_requests):
        await container.breaker.record("mock_alpha", success=False)
    payment = (await pay(client)).json()
    assert statuses(payment) == [("mock_beta", "approved")]
    assert alpha.charge_calls == []
    stats = {s["provider"]: s for s in (await client.get("/stats/providers")).json()}
    assert stats["mock_alpha"]["circuit"]["state"] == "open"
    assert stats["mock_beta"]["approved"] == 1


async def test_provider_adapter_crash_is_treated_as_unknown(
    client: httpx.AsyncClient, alpha: FakeProvider, beta: FakeProvider
) -> None:
    alpha.charge_script = [RuntimeError("bug in adapter")]
    alpha.status_script = [approved()]
    payment = (await pay(client)).json()
    assert statuses(payment) == [("mock_alpha", "approved")]
    assert beta.charge_calls == []


async def test_stats_approval_rate(client: httpx.AsyncClient, alpha: FakeProvider) -> None:
    alpha.charge_script = [soft(), soft()]
    for _ in range(3):
        await pay(client)
    stats = {s["provider"]: s for s in (await client.get("/stats/providers")).json()}
    assert stats["mock_alpha"]["attempts"] == 3
    assert stats["mock_alpha"]["approval_rate"] == round(1 / 3, 4)


async def test_api_validation(client: httpx.AsyncClient) -> None:
    no_key = await client.post("/payments", json={})
    assert no_key.status_code == 422  # body validation runs first
    body = {
        "customer_id": "c",
        "amount": 1999,
        "currency": "USD",
        "country": "DE",
        "payment_method": {"token": "tok_approve"},
    }
    assert (await client.post("/payments", json=body)).status_code == 400  # no Idempotency-Key
    float_amount = await pay(client, amount=19.99)
    assert float_amount.status_code == 422
    card_number = await pay(client, token="4242424242424242")
    assert card_number.status_code == 422
    hidden_card_number = await pay(client, token="tok_4242424242424242")
    assert hidden_card_number.status_code == 422
    assert (await client.get(f"/payments/{'0' * 8}-0000-0000-0000-{'0' * 12}")).status_code == 404
    assert await scalar("SELECT count(*) FROM payments") == 0
