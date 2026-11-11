from enum import StrEnum


class PaymentStatus(StrEnum):
    CREATED = "created"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REQUIRES_REVIEW = "requires_review"


class AttemptStatus(StrEnum):
    PENDING = "pending"  # request to the provider is (or may be) in flight
    APPROVED = "approved"
    DECLINED_SOFT = "declined_soft"
    DECLINED_HARD = "declined_hard"
    ERROR = "error"  # technical error, the provider guarantees nothing was charged
    UNKNOWN = "unknown"  # timeout or ambiguous answer: money may or may not be taken
    REFUND_PENDING = "refund_pending"  # extra charge, refund is scheduled
    REFUNDED = "refunded"


class OutcomeKind(StrEnum):
    """Normalized answer of any provider. Each adapter maps its own codes to these."""

    APPROVED = "approved"
    SOFT_DECLINE = "soft_decline"  # another provider may approve: cascade
    HARD_DECLINE = "hard_decline"  # issuer's final word (fraud, stolen, invalid card...): stop
    TECHNICAL_ERROR = "technical_error"  # provider did not process the request: cascade
    UNKNOWN = "unknown"  # we do not know whether money was taken: never cascade blindly


class PaymentMethodType(StrEnum):
    CARD = "card"


class AlertKind(StrEnum):
    EXTRA_CHARGE = "extra_charge"  # money taken twice for one payment
    LATE_APPROVAL = "late_approval"  # provider approved after reporting a decline/error
    PROVIDER_CONFLICT = "provider_conflict"  # provider contradicts an approval it gave
    REFUND_FAILED = "refund_failed"
    REVIEW_STUCK = "review_stuck"  # outcome still unknown after many status checks
