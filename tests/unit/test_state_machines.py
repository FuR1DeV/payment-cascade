from datetime import timedelta
from itertools import product

import pytest

from app.modules.payments.domain.entities import (
    ATTEMPT_TRANSITIONS,
    PAYMENT_TRANSITIONS,
    PaymentAttempt,
)
from app.modules.payments.domain.enums import AttemptStatus, OutcomeKind, PaymentStatus
from app.modules.payments.domain.errors import InvalidStateTransition
from app.modules.payments.domain.outcome import ProviderOutcome

from .factories import T0, make_payment, processing_payment

A = AttemptStatus
P = PaymentStatus


def attempt_in(status: AttemptStatus) -> PaymentAttempt:
    payment = processing_payment()
    attempt = payment.begin_attempt("a", T0)
    attempt.status = status  # test setup only; production code never assigns directly
    return attempt


@pytest.mark.parametrize(("current", "target"), list(product(A, A)))
def test_attempt_transition_table(current: AttemptStatus, target: AttemptStatus) -> None:
    attempt = attempt_in(current)
    if target in ATTEMPT_TRANSITIONS[current]:
        attempt._transition(target, T0)
        assert attempt.status is target
    else:
        with pytest.raises(InvalidStateTransition):
            attempt._transition(target, T0)


@pytest.mark.parametrize(("current", "target"), list(product(P, P)))
def test_payment_transition_table(current: PaymentStatus, target: PaymentStatus) -> None:
    payment = make_payment()
    payment.status = current
    if target in PAYMENT_TRANSITIONS[current]:
        payment._transition(target, T0)
        assert payment.status is target
    else:
        with pytest.raises(InvalidStateTransition):
            payment._transition(target, T0)


def test_final_payment_states_have_no_exits() -> None:
    assert PAYMENT_TRANSITIONS[P.SUCCEEDED] == frozenset()
    assert PAYMENT_TRANSITIONS[P.FAILED] == frozenset()


def outcome(kind: OutcomeKind, code: str | None = None) -> ProviderOutcome:
    return ProviderOutcome(kind=kind, code=code)


def test_same_outcome_twice_is_noop() -> None:
    attempt = attempt_in(A.PENDING)
    assert attempt.apply_outcome(outcome(OutcomeKind.APPROVED), T0) is True
    assert attempt.apply_outcome(outcome(OutcomeKind.APPROVED), T0) is False
    assert attempt.status is A.APPROVED


@pytest.mark.parametrize("status", [A.APPROVED, A.DECLINED_SOFT, A.ERROR, A.UNKNOWN])
def test_unknown_never_overrides(status: AttemptStatus) -> None:
    attempt = attempt_in(status)
    assert attempt.apply_outcome(outcome(OutcomeKind.UNKNOWN), T0) is False
    assert attempt.status is status


def test_pending_to_unknown_makes_status_check_due() -> None:
    attempt = attempt_in(A.PENDING)
    assert attempt.apply_outcome(outcome(OutcomeKind.UNKNOWN, "timeout"), T0) is True
    assert attempt.status is A.UNKNOWN
    assert attempt.next_status_check_at == T0
    assert attempt.finished_at is None


def test_first_not_charged_answer_wins() -> None:
    attempt = attempt_in(A.PENDING)
    attempt.apply_outcome(outcome(OutcomeKind.SOFT_DECLINE, "05"), T0)
    assert attempt.apply_outcome(outcome(OutcomeKind.HARD_DECLINE, "43"), T0) is False
    assert attempt.status is A.DECLINED_SOFT
    assert attempt.provider_code == "05"


@pytest.mark.parametrize("status", [A.DECLINED_SOFT, A.DECLINED_HARD, A.ERROR])
def test_approval_after_not_charged_is_a_late_approval(status: AttemptStatus) -> None:
    attempt = attempt_in(status)
    assert attempt.apply_outcome(outcome(OutcomeKind.APPROVED), T0) is True
    assert attempt.status is A.APPROVED
    assert attempt.late_approval is True


def test_status_check_scheduling_only_for_unknown() -> None:
    attempt = attempt_in(A.UNKNOWN)
    attempt.schedule_status_check(T0, timedelta(seconds=5))
    assert attempt.status_checks == 0  # scheduled, not made yet
    assert attempt.next_status_check_at == T0 + timedelta(seconds=5)
