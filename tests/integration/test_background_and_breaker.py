from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis

from app.modules.payments.bootstrap import Container
from app.modules.payments.domain.entities import Payment
from app.modules.payments.domain.enums import AttemptStatus, PaymentMethodType, PaymentStatus
from app.modules.payments.domain.money import Money
from app.modules.payments.infrastructure.circuit_breaker import RedisCircuitBreaker
from tests.fakes import FakeProvider, approved

from .helpers import REDIS_URL


async def test_crashed_cascade_is_recovered_without_a_blind_retry(
    container: Container, alpha: FakeProvider, beta: FakeProvider
) -> None:
    """The process died after sending the request to alpha: the attempt stays 'pending'."""
    past = datetime.now(UTC) - timedelta(minutes=5)
    payment = Payment.create(
        customer_id="c",
        idempotency_key="crash-key-1",
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
    async with container.uow_factory() as uow:
        await uow.payments.insert_if_absent(payment)
        await uow.commit()

    assert await container.recover_stale() == 1
    recovered = await container.queries.get_payment(payment.id)
    assert recovered is not None
    assert recovered.status is PaymentStatus.REQUIRES_REVIEW
    assert recovered.attempts[0].status is AttemptStatus.UNKNOWN
    assert beta.charge_calls == []  # never cascade over an unknown outcome

    alpha.status_script = [approved("alpha_1")]
    await container.reconcile_unknown()
    settled = await container.queries.get_payment(payment.id)
    assert settled is not None
    assert settled.status is PaymentStatus.SUCCEEDED


def breaker(redis: Redis, now: list[float], **kwargs) -> RedisCircuitBreaker:  # type: ignore[no-untyped-def]
    options = {
        "window_seconds": 60,
        "bucket_seconds": 10,
        "min_requests": 4,
        "failure_rate": 0.5,
        "open_seconds": 30,
        "clock": lambda: now[0],
    }
    options.update(kwargs)
    return RedisCircuitBreaker(redis, **options)


async def test_circuit_opens_on_failure_rate_and_closes_after_cooldown() -> None:
    redis = Redis.from_url(REDIS_URL)
    now = [1_000_000.0]
    cb = breaker(redis, now, open_seconds=1)
    try:
        await cb.record("p", success=True)
        await cb.record("p", success=False)
        await cb.record("p", success=True)
        assert await cb.is_available("p")  # 1 of 3, below min_requests
        await cb.record("p", success=False)  # 2 of 4 = 50%
        assert not await cb.is_available("p")
        assert (await cb.state("p"))["state"] == "open"
        await redis.delete("cb:p:open")  # as if the cooldown TTL expired
        assert await cb.is_available("p")
        assert (await cb.state("p"))["window_requests"] == 0  # fresh window after opening
    finally:
        await redis.aclose()


async def test_old_failures_leave_the_window() -> None:
    redis = Redis.from_url(REDIS_URL)
    now = [2_000_000.0]
    cb = breaker(redis, now)
    try:
        for _ in range(3):
            await cb.record("p", success=False)
        now[0] += 120  # two minutes later
        await cb.record("p", success=False)
        assert await cb.is_available("p")
    finally:
        await redis.aclose()


async def test_breaker_fails_open_when_redis_is_down() -> None:
    redis = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    cb = breaker(redis, [0.0])
    try:
        assert await cb.is_available("p")
        await cb.record("p", success=False)  # does not raise
    finally:
        await redis.aclose()
