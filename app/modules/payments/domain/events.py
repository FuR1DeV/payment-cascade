from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True)
class DomainEvent:
    payment_id: UUID
    occurred_at: datetime


@dataclass(frozen=True)
class PaymentSucceeded(DomainEvent):
    attempt_no: int
    provider: str


@dataclass(frozen=True)
class PaymentFailed(DomainEvent):
    reason: str


@dataclass(frozen=True)
class PaymentSentToReview(DomainEvent):
    reason: str


@dataclass(frozen=True)
class ExtraChargeDetected(DomainEvent):
    """A provider took money for an attempt that is not the winning one: refund it."""

    attempt_no: int
    provider: str
    winner_attempt_no: int | None
    payment_status: str
    late_approval: bool


@dataclass(frozen=True)
class LateApprovalAccepted(DomainEvent):
    """A provider approved after first reporting a decline/error, and nobody else charged."""

    attempt_no: int
    provider: str


@dataclass(frozen=True)
class ConflictingOutcomeIgnored(DomainEvent):
    attempt_no: int
    provider: str
    current_status: str
    reported_kind: str
