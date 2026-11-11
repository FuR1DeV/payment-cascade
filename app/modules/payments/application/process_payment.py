"""The cascade loop.

Each step is: lock the payment row, decide, write, commit. Provider calls happen
between transactions, so no database lock or connection is held during network I/O.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from app.core.clock import Clock, utc_now
from app.core.logging import log_context
from app.modules.payments.domain.entities import Payment, PaymentAttempt
from app.modules.payments.domain.enums import AttemptStatus, OutcomeKind, PaymentStatus
from app.modules.payments.domain.outcome import ProviderOutcome
from app.modules.payments.domain.policy import (
    CascadeLimits,
    FailPayment,
    NextStep,
    ResolveUnknown,
    SendToReview,
    TryProvider,
    decide_next_step,
)

from .common import PaymentNotFoundError, charge_request, persist, status_request
from .ports import CircuitBreaker, JobQueue, ProviderRegistry, UnitOfWorkFactory

logger = logging.getLogger("payments.cascade")

type Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class StatusCheckSchedule:
    initial_delay: timedelta = timedelta(seconds=0.5)
    max_delay: timedelta = timedelta(seconds=2)

    def delay(self, checks_done: int) -> timedelta:
        return min(self.initial_delay * (2**checks_done), self.max_delay)


class ProcessPayment:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        providers: ProviderRegistry,
        breaker: CircuitBreaker,
        jobs: JobQueue,
        limits: CascadeLimits,
        status_checks: StatusCheckSchedule,
        clock: Clock = utc_now,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._uow_factory = uow_factory
        self._providers = providers
        self._breaker = breaker
        self._jobs = jobs
        self._limits = limits
        self._status_checks = status_checks
        self._clock = clock
        self._sleep = sleep

    async def __call__(self, payment_id: UUID) -> Payment:
        with log_context(payment_id=str(payment_id)):
            route = await self._route(payment_id)
            while True:
                available = await self._available(route)
                payment, step, attempt = await self._next_step(payment_id, available)
                if attempt is None:
                    return payment
                with log_context(attempt_no=attempt.attempt_no, provider=attempt.provider):
                    if isinstance(step, TryProvider):
                        outcome = await self._charge(payment, attempt)
                    else:
                        outcome = await self._check_status(payment, attempt)
                    await self._record(payment_id, attempt.attempt_no, outcome)

    async def _route(self, payment_id: UUID) -> list[str]:
        async with self._uow_factory() as uow:
            payment = await uow.payments.get(payment_id)
        if payment is None:
            raise PaymentNotFoundError(payment_id)
        return payment.route

    async def _available(self, route: list[str]) -> set[str]:
        available = set()
        for code in route:
            if code in self._providers and await self._breaker.is_available(code):
                available.add(code)
        return available

    async def _next_step(
        self, payment_id: UUID, available: set[str]
    ) -> tuple[Payment, NextStep, PaymentAttempt | None]:
        async with self._uow_factory() as uow:
            payment = await uow.payments.get(payment_id, for_update=True)
            if payment is None:
                raise PaymentNotFoundError(payment_id)
            now = self._clock()
            step = decide_next_step(
                payment, available_providers=available, limits=self._limits, now=now
            )
            attempt: PaymentAttempt | None = None
            match step:
                case TryProvider(provider=provider):
                    if payment.status is PaymentStatus.CREATED:
                        payment.start_processing(now)
                    attempt = payment.begin_attempt(provider, now)
                case ResolveUnknown(attempt_no=attempt_no):
                    # Nothing to write: the status check happens outside the transaction.
                    return payment, step, payment.attempt(attempt_no)
                case FailPayment(reason=reason):
                    payment.fail(reason, now)
                case SendToReview(reason=reason):
                    payment.require_review(reason, now)
                case _:
                    # Done or another process has a request in flight: nothing to do here.
                    return payment, step, None
            await persist(uow, payment, self._jobs)
            return payment, step, attempt

    async def _charge(self, payment: Payment, attempt: PaymentAttempt) -> ProviderOutcome:
        provider = self._providers.get(attempt.provider)
        started = time.monotonic()
        try:
            outcome = await provider.charge(charge_request(payment, attempt))
        except Exception:
            # We cannot tell whether the request reached the provider: treat as unknown.
            logger.exception("provider_adapter_crashed")
            outcome = ProviderOutcome.unknown("adapter_exception")
        healthy = outcome.kind not in (OutcomeKind.TECHNICAL_ERROR, OutcomeKind.UNKNOWN)
        await self._breaker.record(attempt.provider, success=healthy)
        logger.info(
            "attempt_finished",
            extra={
                "outcome": outcome.kind.value,
                "provider_code": outcome.code,
                "latency_ms": round((time.monotonic() - started) * 1000),
            },
        )
        return outcome

    async def _check_status(self, payment: Payment, attempt: PaymentAttempt) -> ProviderOutcome:
        remaining = (payment.deadline_at - self._clock()).total_seconds()
        delay = min(self._status_checks.delay(attempt.status_checks).total_seconds(), remaining)
        if delay > 0:
            # A webhook may resolve the attempt while we wait; _record handles that.
            await self._sleep(delay)
        provider = self._providers.get(attempt.provider)
        try:
            outcome = await provider.get_status(status_request(payment, attempt, self._clock()))
        except Exception:
            logger.exception("provider_status_check_crashed")
            outcome = ProviderOutcome.unknown("adapter_exception")
        logger.info(
            "status_checked",
            extra={"outcome": outcome.kind.value, "provider_code": outcome.code},
        )
        return outcome

    async def _record(self, payment_id: UUID, attempt_no: int, outcome: ProviderOutcome) -> None:
        async with self._uow_factory() as uow:
            payment = await uow.payments.get(payment_id, for_update=True)
            if payment is None:
                raise PaymentNotFoundError(payment_id)
            now = self._clock()
            attempt = payment.attempt(attempt_no)
            was_unknown = attempt.status is AttemptStatus.UNKNOWN
            # Applied even when the answer is "unknown": it may carry the provider's id.
            payment.record_outcome(attempt_no, outcome, now)
            if was_unknown and attempt.status is AttemptStatus.UNKNOWN:
                attempt.schedule_status_check(now, timedelta(0))
            await persist(uow, payment, self._jobs)
