from datetime import timedelta

import pytest

from app.modules.payments.domain.enums import OutcomeKind, PaymentStatus
from app.modules.payments.domain.outcome import ProviderOutcome
from app.modules.payments.domain.policy import (
    CascadeLimits,
    Done,
    FailPayment,
    ResolveUnknown,
    SendToReview,
    TryProvider,
    WaitInFlight,
    decide_next_step,
)

from .factories import T0, make_payment, processing_payment

LIMITS = CascadeLimits(max_attempts=3, min_attempt_budget=timedelta(seconds=3))
ALL = {"a", "b", "c"}


def outcome(kind: OutcomeKind, code: str = "x") -> ProviderOutcome:
    return ProviderOutcome(kind=kind, code=code)


def decide(payment, available=ALL, now=T0):  # type: ignore[no-untyped-def]
    return decide_next_step(payment, available_providers=available, limits=LIMITS, now=now)


def test_first_step_is_first_provider_of_route() -> None:
    assert decide(make_payment()) == TryProvider("a")


@pytest.mark.parametrize("kind", [OutcomeKind.SOFT_DECLINE, OutcomeKind.TECHNICAL_ERROR])
def test_soft_decline_and_technical_error_cascade(kind: OutcomeKind) -> None:
    payment = processing_payment()
    payment.begin_attempt("a", T0)
    payment.record_outcome(1, outcome(kind), T0)
    assert decide(payment) == TryProvider("b")


def test_hard_decline_stops() -> None:
    payment = processing_payment()
    payment.begin_attempt("a", T0)
    payment.record_outcome(1, outcome(OutcomeKind.HARD_DECLINE, "43"), T0)
    assert decide(payment) == FailPayment("hard_decline:a:43")


def test_unknown_is_resolved_at_the_same_provider_never_cascaded() -> None:
    payment = processing_payment()
    payment.begin_attempt("a", T0)
    payment.record_outcome(1, outcome(OutcomeKind.UNKNOWN, "timeout"), T0)
    assert decide(payment) == ResolveUnknown(1)


def test_unknown_after_deadline_goes_to_review() -> None:
    payment = processing_payment()
    payment.begin_attempt("a", T0)
    payment.record_outcome(1, outcome(OutcomeKind.UNKNOWN), T0)
    step = decide(payment, now=payment.deadline_at)
    assert isinstance(step, SendToReview)


def test_pending_attempt_means_someone_else_is_working() -> None:
    payment = processing_payment()
    payment.begin_attempt("a", T0)
    assert decide(payment) == WaitInFlight(1)


def test_unavailable_providers_are_skipped() -> None:
    assert decide(make_payment(), available={"c"}) == TryProvider("c")


def test_no_available_provider() -> None:
    assert decide(make_payment(), available=set()) == FailPayment("no_available_provider")


def test_max_attempts() -> None:
    payment = processing_payment(["a", "b", "c", "d"])
    for no, provider in enumerate(["a", "b", "c"], start=1):
        payment.begin_attempt(provider, T0)
        payment.record_outcome(no, outcome(OutcomeKind.SOFT_DECLINE), T0)
    assert decide(payment, available={"a", "b", "c", "d"}) == FailPayment("max_attempts_reached")


def test_route_exhausted() -> None:
    payment = processing_payment(["a"])
    payment.begin_attempt("a", T0)
    payment.record_outcome(1, outcome(OutcomeKind.SOFT_DECLINE), T0)
    assert decide(payment) == FailPayment("cascade_exhausted")


def test_time_budget() -> None:
    payment = processing_payment(budget=timedelta(seconds=10))
    payment.begin_attempt("a", T0)
    payment.record_outcome(1, outcome(OutcomeKind.SOFT_DECLINE), T0)
    late = T0 + timedelta(seconds=8)  # 2 seconds left, an attempt needs 3
    assert decide(payment, now=late) == FailPayment("time_budget_exhausted")


@pytest.mark.parametrize(
    "status", [PaymentStatus.SUCCEEDED, PaymentStatus.FAILED, PaymentStatus.REQUIRES_REVIEW]
)
def test_nothing_to_do_for_settled_payments(status: PaymentStatus) -> None:
    payment = make_payment()
    payment.status = status
    assert decide(payment) == Done()
