"""Cascade policy: given the payment state, what is the next step. Pure function, no I/O."""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta

from .entities import Payment
from .enums import AttemptStatus, PaymentStatus


@dataclass(frozen=True)
class CascadeLimits:
    max_attempts: int = 3
    # Do not start a new attempt if less than this is left of the payment time budget.
    min_attempt_budget: timedelta = timedelta(seconds=3)


@dataclass(frozen=True)
class TryProvider:
    provider: str


@dataclass(frozen=True)
class ResolveUnknown:
    """Last attempt timed out: ask the same provider for the status. Never go to the next one."""

    attempt_no: int


@dataclass(frozen=True)
class WaitInFlight:
    """Another process has a request in flight for this payment."""

    attempt_no: int


@dataclass(frozen=True)
class FailPayment:
    reason: str


@dataclass(frozen=True)
class SendToReview:
    reason: str


@dataclass(frozen=True)
class Done:
    pass


type NextStep = TryProvider | ResolveUnknown | WaitInFlight | FailPayment | SendToReview | Done


def decide_next_step(
    payment: Payment,
    *,
    available_providers: Collection[str],
    limits: CascadeLimits,
    now: datetime,
) -> NextStep:
    if payment.status not in (PaymentStatus.CREATED, PaymentStatus.PROCESSING):
        return Done()

    in_flight = payment.in_flight_attempt
    if in_flight is not None:
        if in_flight.status is AttemptStatus.PENDING:
            return WaitInFlight(in_flight.attempt_no)
        if now < payment.deadline_at:
            return ResolveUnknown(in_flight.attempt_no)
        return SendToReview(
            f"attempt {in_flight.attempt_no} at {in_flight.provider}: outcome unknown "
            "when the time budget ran out"
        )

    hard = next((a for a in payment.attempts if a.status is AttemptStatus.DECLINED_HARD), None)
    if hard is not None:
        return FailPayment(f"hard_decline:{hard.provider}:{hard.provider_code}")
    if len(payment.attempts) >= limits.max_attempts:
        return FailPayment("max_attempts_reached")
    if payment.deadline_at - now < limits.min_attempt_budget:
        return FailPayment("time_budget_exhausted")

    tried = {a.provider for a in payment.attempts}
    for provider in payment.route:
        if provider not in tried and provider in available_providers:
            return TryProvider(provider)
    if not payment.attempts:
        return FailPayment("no_available_provider")
    return FailPayment("cascade_exhausted")
