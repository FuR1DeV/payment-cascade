"""Guards added after the final review: replay only during the cascade, provider values
that do not fit the columns."""

from app.modules.payments.application.common import status_request
from app.modules.payments.domain.entities import (
    PROVIDER_CODE_MAX,
    PROVIDER_MESSAGE_MAX,
    PROVIDER_PAYMENT_ID_MAX,
    REFUND_ID_MAX,
)
from app.modules.payments.domain.enums import OutcomeKind, PaymentStatus
from app.modules.payments.domain.outcome import ProviderOutcome
from app.modules.payments.infrastructure.db.tables import payment_attempts

from .factories import T0, processing_payment

UNKNOWN = ProviderOutcome(kind=OutcomeKind.UNKNOWN, code="timeout")


def test_replay_is_allowed_only_while_the_cascade_runs() -> None:
    payment = processing_payment()
    attempt = payment.begin_attempt("a", T0)
    payment.record_outcome(1, UNKNOWN, T0)
    assert status_request(payment, attempt, T0).replay_allowed is True

    payment.require_review("timeout", T0)
    assert payment.status is PaymentStatus.REQUIRES_REVIEW
    assert status_request(payment, attempt, T0).replay_allowed is False


def test_long_provider_values_are_cut_to_the_column_size() -> None:
    payment = processing_payment()
    payment.begin_attempt("a", T0)
    outcome = ProviderOutcome(
        kind=OutcomeKind.APPROVED,
        provider_payment_id="p" * 300,
        code="c" * 300,
        message="m" * 3000,
    )
    assert payment.record_outcome(1, outcome, T0) is True
    attempt = payment.attempt(1)
    assert len(attempt.provider_payment_id or "") == PROVIDER_PAYMENT_ID_MAX
    assert len(attempt.provider_code or "") == PROVIDER_CODE_MAX
    assert len(attempt.provider_message or "") == PROVIDER_MESSAGE_MAX
    assert payment.status is PaymentStatus.SUCCEEDED  # the approval itself is not lost


def test_limits_match_the_database_columns() -> None:
    columns = payment_attempts.c
    assert columns.provider_payment_id.type.length == PROVIDER_PAYMENT_ID_MAX
    assert columns.provider_code.type.length == PROVIDER_CODE_MAX
    assert columns.provider_message.type.length == PROVIDER_MESSAGE_MAX
    assert columns.refund_id.type.length == REFUND_ID_MAX
