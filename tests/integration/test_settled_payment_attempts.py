"""A payment can be settled by one attempt while another attempt's outcome is still open.

Example: A says "not processed", we go to B; while B is in flight, a late webhook says
A did charge, so A wins. If B then times out (or the process dies during B's call), B
may also have taken the money. Nobody must forget about B.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx

from app.modules.payments.bootstrap import Container
from app.modules.payments.domain.entities import Payment
from app.modules.payments.domain.enums import AttemptStatus, PaymentMethodType, PaymentStatus
from app.modules.payments.domain.money import Money
from tests.fakes import FakeProvider, approved, signed_webhook, tech, unknown

from .helpers import pay


def statuses(payment: dict) -> list[tuple[str, str]]:
    return [(a["provider"], a["status"]) for a in payment["attempts"]]


async def test_unknown_attempt_of_settled_payment_is_reconciled_and_refunded(
    client: httpx.AsyncClient, container: Container, alpha: FakeProvider, beta: FakeProvider
) -> None:
    alpha.charge_script = [tech("96")]
    beta.charge_script = [unknown("timeout")]
    beta.delay = 0.5
    request = asyncio.create_task(pay(client))
    for _ in range(200):  # wait until the request to beta is in flight
        if beta.charge_calls:
            break
        await asyncio.sleep(0.01)
    payment_id = beta.charge_calls[0].payment_id

    body, headers = signed_webhook(
        event_id="evt_late", attempt_key=f"{payment_id}:1", kind="approved"
    )
    late = await client.post("/webhooks/mock_alpha", content=body, headers=headers)
    assert late.json() == {"result": "applied"}

    payment = (await request).json()
    assert payment["status"] == "succeeded"
    assert payment["succeeded_provider"] == "mock_alpha"
    assert statuses(payment) == [("mock_alpha", "approved"), ("mock_beta", "unknown")]

    # Beta did charge as well: the background check must find it and refund it.
    beta.status_script = [approved("beta_1")]
    assert await container.reconcile_unknown() == 1
    after = (await client.get(f"/payments/{payment_id}")).json()
    assert statuses(after) == [("mock_alpha", "approved"), ("mock_beta", "refund_pending")]
    alerts = (await client.get("/alerts", params={"payment_id": str(payment_id)})).json()
    assert sorted(a["kind"] for a in alerts) == ["extra_charge", "late_approval"]
    await container.execute_refunds()
    assert len(beta.refund_calls) == 1


async def test_pending_attempt_of_settled_payment_is_recovered_after_crash(
    container: Container, beta: FakeProvider
) -> None:
    past = datetime.now(UTC) - timedelta(minutes=5)
    payment = Payment.create(
        customer_id="c",
        idempotency_key="crash-key-2",
        request_hash="h",
        money=Money(amount_minor=500, currency="USD"),
        country="DE",
        method=PaymentMethodType.CARD,
        payment_token="tok_approve",
        description=None,
        route=["mock_alpha", "mock_beta"],
        now=past,
        time_budget=timedelta(seconds=15),
    )
    payment.start_processing(past)
    payment.begin_attempt("mock_alpha", past)
    payment.record_outcome(1, tech("96"), past)
    payment.begin_attempt("mock_beta", past)  # the process dies during this call
    payment.record_outcome(1, approved("alpha_1"), past)  # late webhook: alpha wins
    payment.pull_events()
    assert payment.status is PaymentStatus.SUCCEEDED
    async with container.uow_factory() as uow:
        await uow.payments.insert_if_absent(payment)
        await uow.commit()

    assert await container.recover_stale() == 1
    recovered = await container.queries.get_payment(payment.id)
    assert recovered is not None
    assert recovered.attempts[1].status is AttemptStatus.UNKNOWN

    beta.status_script = [approved("beta_1")]
    assert await container.reconcile_unknown() == 1
    settled = await container.queries.get_payment(payment.id)
    assert settled is not None
    assert settled.attempts[1].status is AttemptStatus.REFUND_PENDING
