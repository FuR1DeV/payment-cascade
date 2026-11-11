import pytest

from app.modules.payments.domain.enums import AttemptStatus, OutcomeKind, PaymentStatus
from app.modules.payments.domain.errors import AttemptNotAllowed
from app.modules.payments.domain.events import (
    ConflictingOutcomeIgnored,
    ExtraChargeDetected,
    LateApprovalAccepted,
    PaymentSucceeded,
)
from app.modules.payments.domain.outcome import ProviderOutcome

from .factories import T0, make_payment, processing_payment

APPROVED = ProviderOutcome(kind=OutcomeKind.APPROVED, provider_payment_id="p1")
SOFT = ProviderOutcome(kind=OutcomeKind.SOFT_DECLINE, code="05")
ERROR = ProviderOutcome(kind=OutcomeKind.TECHNICAL_ERROR, code="96")
UNKNOWN = ProviderOutcome(kind=OutcomeKind.UNKNOWN, code="timeout")


def test_attempt_needs_processing_payment() -> None:
    with pytest.raises(AttemptNotAllowed):
        make_payment().begin_attempt("a", T0)


def test_attempt_idempotency_key_is_payment_id_and_number() -> None:
    payment = processing_payment()
    first = payment.begin_attempt("a", T0)
    payment.record_outcome(1, SOFT, T0)
    second = payment.begin_attempt("b", T0)
    assert (first.idempotency_key, second.idempotency_key) == (
        f"{payment.id}:1",
        f"{payment.id}:2",
    )


@pytest.mark.parametrize("outcome", [None, UNKNOWN])
def test_no_new_attempt_while_one_is_in_flight(outcome: ProviderOutcome | None) -> None:
    payment = processing_payment()
    payment.begin_attempt("a", T0)
    if outcome is not None:
        payment.record_outcome(1, outcome, T0)
    with pytest.raises(AttemptNotAllowed, match="in flight"):
        payment.begin_attempt("b", T0)


def test_same_provider_is_not_tried_twice() -> None:
    payment = processing_payment()
    payment.begin_attempt("a", T0)
    payment.record_outcome(1, SOFT, T0)
    with pytest.raises(AttemptNotAllowed, match="already tried"):
        payment.begin_attempt("a", T0)


def test_approval_settles_payment() -> None:
    payment = processing_payment()
    payment.begin_attempt("a", T0)
    assert payment.record_outcome(1, APPROVED, T0) is True
    assert payment.status is PaymentStatus.SUCCEEDED
    assert payment.succeeded_attempt_no == 1
    assert [type(e) for e in payment.pull_events()] == [PaymentSucceeded]


def test_second_approval_is_an_extra_charge_and_gets_refunded() -> None:
    payment = processing_payment()
    payment.begin_attempt("a", T0)
    payment.record_outcome(1, ERROR, T0)
    payment.begin_attempt("b", T0)
    payment.record_outcome(2, APPROVED, T0)
    payment.pull_events()

    # Late webhook: "a" did charge after all.
    assert payment.record_outcome(1, APPROVED, T0) is True
    a = payment.attempt(1)
    assert a.status is AttemptStatus.REFUND_PENDING
    assert a.late_approval is True
    assert payment.status is PaymentStatus.SUCCEEDED
    assert payment.succeeded_attempt_no == 2
    events = payment.pull_events()
    assert len(events) == 1
    assert isinstance(events[0], ExtraChargeDetected)
    assert events[0].winner_attempt_no == 2


def test_late_approval_wins_if_nobody_else_charged() -> None:
    payment = processing_payment()
    payment.begin_attempt("a", T0)
    payment.record_outcome(1, ERROR, T0)
    payment.begin_attempt("b", T0)  # in flight when the late webhook for "a" arrives
    payment.record_outcome(1, APPROVED, T0)
    assert payment.status is PaymentStatus.SUCCEEDED
    assert payment.succeeded_attempt_no == 1
    assert {type(e) for e in payment.pull_events()} == {PaymentSucceeded, LateApprovalAccepted}

    # Then "b" also approves: that one is the extra charge.
    payment.record_outcome(2, APPROVED, T0)
    assert payment.attempt(2).status is AttemptStatus.REFUND_PENDING


def test_approval_for_failed_payment_is_refunded() -> None:
    payment = processing_payment(["a"])
    payment.begin_attempt("a", T0)
    payment.record_outcome(1, SOFT, T0)
    payment.fail("cascade_exhausted", T0)
    payment.record_outcome(1, APPROVED, T0)
    assert payment.status is PaymentStatus.FAILED
    assert payment.attempt(1).status is AttemptStatus.REFUND_PENDING


def test_decline_after_approval_is_ignored_as_conflict() -> None:
    payment = processing_payment()
    payment.begin_attempt("a", T0)
    payment.record_outcome(1, APPROVED, T0)
    payment.pull_events()
    assert payment.record_outcome(1, SOFT, T0) is False
    assert payment.attempt(1).status is AttemptStatus.APPROVED
    assert [type(e) for e in payment.pull_events()] == [ConflictingOutcomeIgnored]


def test_cannot_fail_with_unknown_attempt() -> None:
    payment = processing_payment()
    payment.begin_attempt("a", T0)
    payment.record_outcome(1, UNKNOWN, T0)
    with pytest.raises(AttemptNotAllowed):
        payment.fail("whatever", T0)


def test_review_resolves_to_success_or_failure() -> None:
    charged = processing_payment()
    charged.begin_attempt("a", T0)
    charged.record_outcome(1, UNKNOWN, T0)
    charged.require_review("timeout", T0)
    charged.record_outcome(1, APPROVED, T0)
    assert charged.status is PaymentStatus.SUCCEEDED

    not_charged = processing_payment()
    not_charged.begin_attempt("a", T0)
    not_charged.record_outcome(1, UNKNOWN, T0)
    not_charged.require_review("timeout", T0)
    assert not_charged.resolve_review(T0) is False  # still unknown
    # The answer may come from a status check or a webhook: either way the review ends.
    not_charged.record_outcome(1, ERROR, T0)
    assert not_charged.status is PaymentStatus.FAILED
    assert not_charged.failure_reason == "not_charged_after_review"
