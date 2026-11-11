"""Payment aggregate. Every state change goes through a method; a forbidden transition raises."""

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from .enums import AlertKind, AttemptStatus, OutcomeKind, PaymentMethodType, PaymentStatus
from .errors import (
    AttemptNotAllowed,
    AttemptNotFound,
    ConflictingProviderOutcome,
    InvalidStateTransition,
)
from .events import (
    ConflictingOutcomeIgnored,
    DomainEvent,
    ExtraChargeDetected,
    LateApprovalAccepted,
    PaymentFailed,
    PaymentSentToReview,
    PaymentSucceeded,
)
from .money import Money
from .outcome import ProviderOutcome

A = AttemptStatus
P = PaymentStatus

ATTEMPT_TRANSITIONS: dict[AttemptStatus, frozenset[AttemptStatus]] = {
    A.PENDING: frozenset({A.APPROVED, A.DECLINED_SOFT, A.DECLINED_HARD, A.ERROR, A.UNKNOWN}),
    A.UNKNOWN: frozenset({A.APPROVED, A.DECLINED_SOFT, A.DECLINED_HARD, A.ERROR}),
    # A provider may report a charge after it told us nothing happened (late webhook).
    # Money moved, so we must record it. The payment then decides whether to refund it.
    A.DECLINED_SOFT: frozenset({A.APPROVED}),
    A.DECLINED_HARD: frozenset({A.APPROVED}),
    A.ERROR: frozenset({A.APPROVED}),
    A.APPROVED: frozenset({A.REFUND_PENDING}),
    A.REFUND_PENDING: frozenset({A.REFUNDED}),
    A.REFUNDED: frozenset(),
}

PAYMENT_TRANSITIONS: dict[PaymentStatus, frozenset[PaymentStatus]] = {
    P.CREATED: frozenset({P.PROCESSING, P.FAILED}),
    P.PROCESSING: frozenset({P.SUCCEEDED, P.FAILED, P.REQUIRES_REVIEW}),
    P.REQUIRES_REVIEW: frozenset({P.SUCCEEDED, P.FAILED}),
    P.SUCCEEDED: frozenset(),
    P.FAILED: frozenset(),
}

IN_FLIGHT = frozenset({A.PENDING, A.UNKNOWN})
MONEY_TAKEN = frozenset({A.APPROVED, A.REFUND_PENDING, A.REFUNDED})
NOT_CHARGED = frozenset({A.DECLINED_SOFT, A.DECLINED_HARD, A.ERROR})

OUTCOME_TO_STATUS: dict[OutcomeKind, AttemptStatus] = {
    OutcomeKind.APPROVED: A.APPROVED,
    OutcomeKind.SOFT_DECLINE: A.DECLINED_SOFT,
    OutcomeKind.HARD_DECLINE: A.DECLINED_HARD,
    OutcomeKind.TECHNICAL_ERROR: A.ERROR,
    OutcomeKind.UNKNOWN: A.UNKNOWN,
}


class PaymentAttempt(BaseModel):
    """One request to one provider. Its idempotency key is stable: payment_id + attempt_no."""

    model_config = ConfigDict(validate_assignment=True)

    id: UUID
    payment_id: UUID
    attempt_no: int = Field(ge=1)
    provider: str
    idempotency_key: str
    status: AttemptStatus = A.PENDING
    provider_payment_id: str | None = None
    provider_code: str | None = None
    provider_message: str | None = None
    late_approval: bool = False
    status_checks: int = 0
    next_status_check_at: datetime | None = None
    refund_id: str | None = None
    refund_attempts: int = 0
    next_refund_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None

    @property
    def in_flight(self) -> bool:
        return self.status in IN_FLIGHT

    @property
    def money_taken(self) -> bool:
        return self.status in MONEY_TAKEN

    def _transition(self, target: AttemptStatus, now: datetime) -> None:
        if target not in ATTEMPT_TRANSITIONS[self.status]:
            raise InvalidStateTransition("attempt", self.status, target)
        self.status = target
        self.updated_at = now
        if target not in IN_FLIGHT and self.finished_at is None:
            self.finished_at = now

    def apply_outcome(self, outcome: ProviderOutcome, now: datetime) -> bool:
        """Apply what the provider said. Returns True if the status changed.

        Rules, in order:
        - the same status again (webhook after the sync response) is a no-op;
        - "unknown" never overrides anything except "pending";
        - once money is taken, a later "not charged" is a conflict, not a transition;
        - between two "not charged" answers the first one wins (no money implications);
        - "approved" after "not charged" is accepted and flagged as a late approval.
        """
        if outcome.provider_payment_id and not self.provider_payment_id:
            self.provider_payment_id = outcome.provider_payment_id
        target = OUTCOME_TO_STATUS[outcome.kind]
        if target == self.status:
            return False
        if target is A.UNKNOWN:
            if self.status is not A.PENDING:
                return False
            self._transition(A.UNKNOWN, now)
            self.next_status_check_at = now
            self._set_code(outcome)
            return True
        if self.status in MONEY_TAKEN:
            if target is A.APPROVED:
                return False
            raise ConflictingProviderOutcome(
                f"attempt {self.attempt_no}: provider reported {outcome.kind.value} "
                f"after approval (status {self.status.value})"
            )
        if self.status in NOT_CHARGED and target in NOT_CHARGED:
            return False
        if self.status in NOT_CHARGED and target is A.APPROVED:
            self.late_approval = True
        self._transition(target, now)
        self.next_status_check_at = None
        self._set_code(outcome)
        return True

    def _set_code(self, outcome: ProviderOutcome) -> None:
        if outcome.code is not None:
            self.provider_code = outcome.code
        if outcome.message is not None:
            self.provider_message = outcome.message[:500]

    def schedule_status_check(self, now: datetime, delay: timedelta) -> None:
        if self.status is not A.UNKNOWN:
            raise AttemptNotAllowed(
                f"attempt {self.attempt_no} is {self.status.value}, not unknown"
            )
        self.status_checks += 1
        self.next_status_check_at = now + delay
        self.updated_at = now

    def request_refund(self, now: datetime) -> None:
        self._transition(A.REFUND_PENDING, now)
        self.next_refund_at = now

    def mark_refunded(self, refund_id: str | None, now: datetime) -> None:
        self._transition(A.REFUNDED, now)
        self.refund_id = refund_id
        self.next_refund_at = None

    def schedule_refund_retry(self, now: datetime, delay: timedelta) -> None:
        if self.status is not A.REFUND_PENDING:
            raise AttemptNotAllowed(f"attempt {self.attempt_no} is {self.status.value}")
        self.refund_attempts += 1
        self.next_refund_at = now + delay
        self.updated_at = now


class Alert(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    payment_id: UUID
    attempt_no: int | None = None
    kind: AlertKind
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class Payment(BaseModel):
    """Aggregate root. Attempts are changed only through the payment, under its row lock."""

    model_config = ConfigDict(validate_assignment=True)

    id: UUID
    customer_id: str
    idempotency_key: str
    request_hash: str
    money: Money
    country: str
    method: PaymentMethodType
    payment_token: str
    description: str | None = None
    status: PaymentStatus = P.CREATED
    route: list[str]
    attempts: list[PaymentAttempt] = Field(default_factory=list)
    succeeded_attempt_no: int | None = None
    failure_reason: str | None = None
    review_reason: str | None = None
    created_at: datetime
    updated_at: datetime
    deadline_at: datetime

    _events: list[DomainEvent] = PrivateAttr(default_factory=list)

    @classmethod
    def create(
        cls,
        *,
        customer_id: str,
        idempotency_key: str,
        request_hash: str,
        money: Money,
        country: str,
        method: PaymentMethodType,
        payment_token: str,
        description: str | None,
        route: list[str],
        now: datetime,
        time_budget: timedelta,
    ) -> "Payment":
        return cls(
            id=uuid4(),
            customer_id=customer_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            money=money,
            country=country,
            method=method,
            payment_token=payment_token,
            description=description,
            route=list(route),
            created_at=now,
            updated_at=now,
            deadline_at=now + time_budget,
        )

    # --- queries -------------------------------------------------------------------------

    @property
    def is_final(self) -> bool:
        return self.status in (P.SUCCEEDED, P.FAILED)

    @property
    def in_flight_attempt(self) -> PaymentAttempt | None:
        return next((a for a in self.attempts if a.in_flight), None)

    @property
    def winner(self) -> PaymentAttempt | None:
        if self.succeeded_attempt_no is None:
            return None
        return self.attempt(self.succeeded_attempt_no)

    def attempt(self, attempt_no: int) -> PaymentAttempt:
        for attempt in self.attempts:
            if attempt.attempt_no == attempt_no:
                return attempt
        raise AttemptNotFound(f"payment {self.id} has no attempt {attempt_no}")

    def find_attempt(
        self, *, attempt_key: str | None = None, provider_payment_id: str | None = None
    ) -> PaymentAttempt | None:
        for attempt in self.attempts:
            if attempt_key is not None and attempt.idempotency_key == attempt_key:
                return attempt
            if (
                provider_payment_id is not None
                and attempt.provider_payment_id == provider_payment_id
            ):
                return attempt
        return None

    # --- commands ------------------------------------------------------------------------

    def _transition(self, target: PaymentStatus, now: datetime) -> None:
        if target not in PAYMENT_TRANSITIONS[self.status]:
            raise InvalidStateTransition("payment", self.status, target)
        self.status = target
        self.updated_at = now

    def start_processing(self, now: datetime) -> None:
        self._transition(P.PROCESSING, now)

    def begin_attempt(self, provider: str, now: datetime) -> PaymentAttempt:
        if self.status is not P.PROCESSING:
            raise AttemptNotAllowed(f"payment is {self.status.value}, not processing")
        if self.in_flight_attempt is not None:
            raise AttemptNotAllowed("another attempt is in flight; its outcome must be known first")
        if any(a.provider == provider for a in self.attempts):
            raise AttemptNotAllowed(f"provider {provider} was already tried")
        attempt_no = len(self.attempts) + 1
        attempt = PaymentAttempt(
            id=uuid4(),
            payment_id=self.id,
            attempt_no=attempt_no,
            provider=provider,
            idempotency_key=f"{self.id}:{attempt_no}",
            created_at=now,
            updated_at=now,
        )
        self.attempts.append(attempt)
        self.updated_at = now
        return attempt

    def record_outcome(self, attempt_no: int, outcome: ProviderOutcome, now: datetime) -> bool:
        """Apply a provider answer (sync response, status check or webhook)."""
        attempt = self.attempt(attempt_no)
        try:
            changed = attempt.apply_outcome(outcome, now)
        except ConflictingProviderOutcome:
            self._events.append(
                ConflictingOutcomeIgnored(
                    payment_id=self.id,
                    occurred_at=now,
                    attempt_no=attempt_no,
                    provider=attempt.provider,
                    current_status=attempt.status.value,
                    reported_kind=outcome.kind.value,
                )
            )
            return False
        if changed:
            self.updated_at = now
            if attempt.status is A.APPROVED:
                self._on_approved(attempt, now)
            else:
                # Whoever brought the answer (status check or webhook), a review whose
                # last open attempt turned out "not charged" is over.
                self.resolve_review(now)
        return changed

    def _on_approved(self, attempt: PaymentAttempt, now: datetime) -> None:
        # The first approval recorded under the payment lock wins.
        if self.succeeded_attempt_no is None and self.status in (P.PROCESSING, P.REQUIRES_REVIEW):
            self.succeeded_attempt_no = attempt.attempt_no
            self._transition(P.SUCCEEDED, now)
            self._events.append(
                PaymentSucceeded(
                    payment_id=self.id,
                    occurred_at=now,
                    attempt_no=attempt.attempt_no,
                    provider=attempt.provider,
                )
            )
            if attempt.late_approval:
                self._events.append(
                    LateApprovalAccepted(
                        payment_id=self.id,
                        occurred_at=now,
                        attempt_no=attempt.attempt_no,
                        provider=attempt.provider,
                    )
                )
            return
        # Someone else already took the money, or the customer was told "failed": compensate.
        attempt.request_refund(now)
        self._events.append(
            ExtraChargeDetected(
                payment_id=self.id,
                occurred_at=now,
                attempt_no=attempt.attempt_no,
                provider=attempt.provider,
                winner_attempt_no=self.succeeded_attempt_no,
                payment_status=self.status.value,
                late_approval=attempt.late_approval,
            )
        )

    def fail(self, reason: str, now: datetime) -> None:
        if self.in_flight_attempt is not None:
            raise AttemptNotAllowed("cannot fail a payment while an attempt outcome is unknown")
        self._transition(P.FAILED, now)
        self.failure_reason = reason
        self._events.append(PaymentFailed(payment_id=self.id, occurred_at=now, reason=reason))

    def require_review(self, reason: str, now: datetime) -> None:
        self._transition(P.REQUIRES_REVIEW, now)
        self.review_reason = reason
        self._events.append(PaymentSentToReview(payment_id=self.id, occurred_at=now, reason=reason))

    def resolve_review(self, now: datetime) -> bool:
        """After review: nothing in flight and nobody charged means the payment failed."""
        if self.status is not P.REQUIRES_REVIEW or self.in_flight_attempt is not None:
            return False
        if self.succeeded_attempt_no is not None:
            return False
        self.fail("not_charged_after_review", now)
        return True

    def pull_events(self) -> list[DomainEvent]:
        events, self._events = self._events, []
        return events
