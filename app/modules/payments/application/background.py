"""Background use cases run by Taskiq: status checks, refunds, recovery after crashes.

The database is the source of truth. Work is claimed with a short lease
(UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED)), so several workers
can run the same task without doing the same work, and a lost task is picked up
again by the next periodic run.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from app.core.clock import Clock, utc_now
from app.core.logging import log_context
from app.modules.payments.domain.entities import Alert
from app.modules.payments.domain.enums import AlertKind, AttemptStatus, OutcomeKind, PaymentStatus
from app.modules.payments.domain.outcome import ProviderOutcome

from .common import backoff, persist, status_request
from .ports import (
    AttemptRef,
    JobQueue,
    ProviderRegistry,
    RefundRequest,
    RefundResult,
    UnitOfWorkFactory,
)
from .process_payment import ProcessPayment

logger = logging.getLogger("payments.background")


@dataclass(frozen=True)
class RetryPolicy:
    base_seconds: float
    max_seconds: float
    alert_after: int


class ReconcileUnknownAttempts:
    """Payments in requires_review: keep asking the provider until the outcome is known."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        providers: ProviderRegistry,
        jobs: JobQueue,
        retry: RetryPolicy,
        lease: timedelta,
        clock: Clock = utc_now,
    ) -> None:
        self._uow_factory = uow_factory
        self._providers = providers
        self._jobs = jobs
        self._retry = retry
        self._lease = lease
        self._clock = clock

    async def __call__(self, limit: int = 100) -> int:
        now = self._clock()
        async with self._uow_factory() as uow:
            claims = await uow.payments.claim_due_status_checks(now, now + self._lease, limit)
            await uow.commit()
        for ref in claims:
            with log_context(payment_id=str(ref.payment_id), attempt_no=ref.attempt_no):
                try:
                    await self._check(ref)
                except Exception:
                    logger.exception("reconcile_failed")
        return len(claims)

    async def _check(self, ref: AttemptRef) -> None:
        async with self._uow_factory() as uow:
            payment = await uow.payments.get(ref.payment_id)
        if payment is None:
            return
        attempt = payment.attempt(ref.attempt_no)
        if attempt.status is not AttemptStatus.UNKNOWN:
            return
        try:
            outcome = await self._providers.get(attempt.provider).get_status(
                status_request(payment, attempt)
            )
        except Exception:
            logger.exception("provider_status_check_crashed")
            outcome = ProviderOutcome.unknown("adapter_exception")
        logger.info(
            "status_checked", extra={"outcome": outcome.kind.value, "provider": attempt.provider}
        )

        async with self._uow_factory() as uow:
            payment = await uow.payments.get(ref.payment_id, for_update=True)
            assert payment is not None  # noqa: S101
            attempt = payment.attempt(ref.attempt_no)
            now = self._clock()
            alerts: list[Alert] = []
            if attempt.status is AttemptStatus.UNKNOWN and outcome.kind is OutcomeKind.UNKNOWN:
                # The delay grows with the age of the attempt: frequent checks in the first
                # minutes, when most cases resolve, rare ones later. Age rather than the check
                # count, because the inline loop has already made several quick checks.
                age = (now - attempt.created_at).total_seconds()
                delay = timedelta(
                    seconds=min(max(self._retry.base_seconds, age / 2), self._retry.max_seconds)
                )
                attempt.schedule_status_check(now, delay)
                if attempt.status_checks == self._retry.alert_after:
                    alerts.append(
                        Alert(
                            payment_id=payment.id,
                            attempt_no=attempt.attempt_no,
                            kind=AlertKind.REVIEW_STUCK,
                            message=(
                                f"{attempt.provider}: outcome still unknown after "
                                f"{attempt.status_checks} status checks; manual review needed"
                            ),
                            created_at=now,
                        )
                    )
            else:
                payment.record_outcome(attempt.attempt_no, outcome, now)
            payment.resolve_review(now)
            await persist(uow, payment, self._jobs, alerts)


class ExecutePendingRefunds:
    """Refund extra charges. Refund idempotency key is derived from the attempt key."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        providers: ProviderRegistry,
        retry: RetryPolicy,
        lease: timedelta,
        clock: Clock = utc_now,
    ) -> None:
        self._uow_factory = uow_factory
        self._providers = providers
        self._retry = retry
        self._lease = lease
        self._clock = clock

    async def __call__(self, limit: int = 100) -> int:
        now = self._clock()
        async with self._uow_factory() as uow:
            claims = await uow.payments.claim_due_refunds(now, now + self._lease, limit)
            await uow.commit()
        for ref in claims:
            with log_context(payment_id=str(ref.payment_id), attempt_no=ref.attempt_no):
                try:
                    await self._refund(ref)
                except Exception:
                    logger.exception("refund_failed_unexpectedly")
        return len(claims)

    async def _refund(self, ref: AttemptRef) -> None:
        async with self._uow_factory() as uow:
            payment = await uow.payments.get(ref.payment_id)
        if payment is None:
            return
        attempt = payment.attempt(ref.attempt_no)
        if attempt.status is not AttemptStatus.REFUND_PENDING:
            return
        request = RefundRequest(
            idempotency_key=f"refund:{attempt.idempotency_key}",
            attempt_key=attempt.idempotency_key,
            provider_payment_id=attempt.provider_payment_id,
            money=payment.money,
        )
        try:
            result = await self._providers.get(attempt.provider).refund(request)
        except Exception as exc:
            logger.exception("provider_refund_crashed")
            result = RefundResult(succeeded=False, error=type(exc).__name__)

        async with self._uow_factory() as uow:
            payment = await uow.payments.get(ref.payment_id, for_update=True)
            assert payment is not None  # noqa: S101
            attempt = payment.attempt(ref.attempt_no)
            if attempt.status is not AttemptStatus.REFUND_PENDING:
                return
            now = self._clock()
            alerts: list[Alert] = []
            if result.succeeded:
                attempt.mark_refunded(result.refund_id, now)
                logger.info("refund_succeeded", extra={"refund_id": result.refund_id})
            else:
                delay = backoff(
                    self._retry.base_seconds, attempt.refund_attempts, self._retry.max_seconds
                )
                attempt.schedule_refund_retry(now, delay)
                logger.warning("refund_retry_scheduled", extra={"error": result.error})
                if attempt.refund_attempts == self._retry.alert_after:
                    alerts.append(
                        Alert(
                            payment_id=payment.id,
                            attempt_no=attempt.attempt_no,
                            kind=AlertKind.REFUND_FAILED,
                            message=(
                                f"refund at {attempt.provider} failed {attempt.refund_attempts} "
                                f"times: {result.error}"
                            ),
                            created_at=now,
                        )
                    )
            await persist(uow, payment, None, alerts)


class RecoverStalePayments:
    """A payment stuck in created/processing long after its deadline means the process that
    ran the cascade died. A pending attempt then may or may not have reached the provider,
    so it becomes unknown, and the regular cascade logic takes it from there."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        process_payment: ProcessPayment,
        jobs: JobQueue,
        grace: timedelta,
        clock: Clock = utc_now,
    ) -> None:
        self._uow_factory = uow_factory
        self._process_payment = process_payment
        self._jobs = jobs
        self._grace = grace
        self._clock = clock

    async def __call__(self, limit: int = 50) -> int:
        async with self._uow_factory() as uow:
            stale = await uow.payments.find_stale(self._clock() - self._grace, limit)
        for payment_id in stale:
            with log_context(payment_id=str(payment_id)):
                try:
                    await self._recover(payment_id)
                except Exception:
                    logger.exception("stale_recovery_failed")
        return len(stale)

    async def _recover(self, payment_id: UUID) -> None:
        async with self._uow_factory() as uow:
            payment = await uow.payments.get(payment_id, for_update=True)
            if payment is None or payment.status not in (
                PaymentStatus.CREATED,
                PaymentStatus.PROCESSING,
            ):
                return
            now = self._clock()
            if payment.deadline_at + self._grace > now:
                return
            for attempt in payment.attempts:
                if attempt.status is AttemptStatus.PENDING:
                    payment.record_outcome(
                        attempt.attempt_no, ProviderOutcome.unknown("stale_pending"), now
                    )
            logger.warning("stale_payment_recovered")
            await persist(uow, payment, self._jobs)
        await self._process_payment(payment_id)
