"""Helpers shared by use cases: saving the aggregate, turning events into alerts."""

import logging
from collections.abc import Iterable
from datetime import timedelta

from app.modules.payments.domain.entities import Alert, Payment, PaymentAttempt
from app.modules.payments.domain.enums import AlertKind
from app.modules.payments.domain.events import (
    ConflictingOutcomeIgnored,
    DomainEvent,
    ExtraChargeDetected,
    LateApprovalAccepted,
)

from .ports import ChargeRequest, JobQueue, StatusRequest, UnitOfWork

logger = logging.getLogger("payments")


class PaymentNotFoundError(LookupError):
    pass


def alerts_from_events(events: Iterable[DomainEvent]) -> list[Alert]:
    alerts: list[Alert] = []
    for event in events:
        match event:
            case ExtraChargeDetected():
                alerts.append(
                    Alert(
                        payment_id=event.payment_id,
                        attempt_no=event.attempt_no,
                        kind=AlertKind.EXTRA_CHARGE,
                        message=(
                            f"{event.provider} charged attempt {event.attempt_no} but the payment "
                            f"is already {event.payment_status}; refund scheduled"
                        ),
                        details={
                            "provider": event.provider,
                            "winner_attempt_no": event.winner_attempt_no,
                            "late_approval": event.late_approval,
                        },
                        created_at=event.occurred_at,
                    )
                )
            case LateApprovalAccepted():
                alerts.append(
                    Alert(
                        payment_id=event.payment_id,
                        attempt_no=event.attempt_no,
                        kind=AlertKind.LATE_APPROVAL,
                        message=(
                            f"{event.provider} approved attempt {event.attempt_no} after "
                            "reporting it as not charged; accepted as the winning attempt"
                        ),
                        details={"provider": event.provider},
                        created_at=event.occurred_at,
                    )
                )
            case ConflictingOutcomeIgnored():
                alerts.append(
                    Alert(
                        payment_id=event.payment_id,
                        attempt_no=event.attempt_no,
                        kind=AlertKind.PROVIDER_CONFLICT,
                        message=(
                            f"{event.provider} reported {event.reported_kind} for attempt "
                            f"{event.attempt_no} which is {event.current_status}; ignored"
                        ),
                        details={"provider": event.provider},
                        created_at=event.occurred_at,
                    )
                )
            case _:
                pass
    return alerts


async def persist(
    uow: UnitOfWork,
    payment: Payment,
    jobs: JobQueue | None,
    extra_alerts: Iterable[Alert] = (),
) -> list[DomainEvent]:
    """Save the aggregate and its alerts in the current transaction, commit, then react."""
    events = payment.pull_events()
    alerts = [*alerts_from_events(events), *extra_alerts]
    await uow.payments.save(payment)
    for alert in alerts:
        await uow.alerts.add(alert)
    await uow.commit()

    for event in events:
        logger.info(type(event).__name__, extra={"payment_id": str(payment.id), **_fields(event)})
    for alert in alerts:
        logger.error(
            "alert",
            extra={
                "payment_id": str(alert.payment_id),
                "alert_kind": alert.kind.value,
                "attempt_no": alert.attempt_no,
                "alert_message": alert.message,
            },
        )
    if jobs is not None and any(isinstance(e, ExtraChargeDetected) for e in events):
        await kick_refunds(jobs)
    return events


async def kick_refunds(jobs: JobQueue) -> None:
    """Best effort. If the queue is down, the periodic refund task picks it up anyway."""
    try:
        await jobs.kick_refunds()
    except Exception:
        logger.warning("refund_kick_failed", exc_info=True)


def _fields(event: DomainEvent) -> dict[str, object]:
    return {k: v for k, v in vars(event).items() if k not in ("payment_id", "occurred_at")}


def charge_request(payment: Payment, attempt: PaymentAttempt) -> ChargeRequest:
    return ChargeRequest(
        payment_id=payment.id,
        attempt_no=attempt.attempt_no,
        idempotency_key=attempt.idempotency_key,
        money=payment.money,
        payment_token=payment.payment_token,
        country=payment.country,
        description=payment.description,
    )


def status_request(payment: Payment, attempt: PaymentAttempt) -> StatusRequest:
    return StatusRequest(
        charge=charge_request(payment, attempt), provider_payment_id=attempt.provider_payment_id
    )


def backoff(base: float, attempt: int, maximum: float) -> timedelta:
    return timedelta(seconds=min(base * (2 ** min(attempt, 16)), maximum))
