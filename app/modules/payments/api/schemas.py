from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.modules.payments.domain.entities import Alert, Payment, PaymentAttempt


class PaymentMethodIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["card"] = "card"
    # Opaque token from the vault. Card numbers are never accepted.
    token: str = Field(pattern=r"^tok_[a-z0-9_]{1,60}$", examples=["tok_soft_decline"])


class CreatePaymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: str = Field(min_length=1, max_length=64, examples=["cust_42"])
    amount: int = Field(
        strict=True, gt=0, le=10**12, description="Minor units (cents)", examples=[1999]
    )
    currency: str = Field(pattern=r"^[A-Z]{3}$", examples=["USD"])
    country: str = Field(pattern=r"^[A-Z]{2}$", examples=["DE"])
    payment_method: PaymentMethodIn
    description: str | None = Field(default=None, max_length=255)


class AttemptOut(BaseModel):
    attempt_no: int
    provider: str
    status: str
    provider_payment_id: str | None
    provider_code: str | None
    provider_message: str | None
    late_approval: bool
    status_checks: int
    refund_id: str | None
    created_at: datetime
    finished_at: datetime | None

    @classmethod
    def of(cls, a: PaymentAttempt) -> "AttemptOut":
        return cls(
            attempt_no=a.attempt_no,
            provider=a.provider,
            status=a.status.value,
            provider_payment_id=a.provider_payment_id,
            provider_code=a.provider_code,
            provider_message=a.provider_message,
            late_approval=a.late_approval,
            status_checks=a.status_checks,
            refund_id=a.refund_id,
            created_at=a.created_at,
            finished_at=a.finished_at,
        )


class PaymentOut(BaseModel):
    id: UUID
    status: str
    customer_id: str
    amount: int
    currency: str
    country: str
    route: list[str]
    succeeded_attempt_no: int | None
    succeeded_provider: str | None
    failure_reason: str | None
    review_reason: str | None
    attempts: list[AttemptOut]
    created_at: datetime
    updated_at: datetime
    deadline_at: datetime

    @classmethod
    def of(cls, p: Payment) -> "PaymentOut":
        winner = p.winner
        return cls(
            id=p.id,
            status=p.status.value,
            customer_id=p.customer_id,
            amount=p.money.amount_minor,
            currency=p.money.currency,
            country=p.country,
            route=p.route,
            succeeded_attempt_no=p.succeeded_attempt_no,
            succeeded_provider=winner.provider if winner else None,
            failure_reason=p.failure_reason,
            review_reason=p.review_reason,
            attempts=[AttemptOut.of(a) for a in p.attempts],
            created_at=p.created_at,
            updated_at=p.updated_at,
            deadline_at=p.deadline_at,
        )


class AlertOut(BaseModel):
    id: UUID
    payment_id: UUID
    attempt_no: int | None
    kind: str
    message: str
    details: dict[str, Any]
    created_at: datetime

    @classmethod
    def of(cls, a: Alert) -> "AlertOut":
        return cls(**a.model_dump(exclude={"kind"}), kind=a.kind.value)


class ErrorOut(BaseModel):
    code: str
    message: str
