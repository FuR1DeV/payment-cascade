from datetime import UTC, datetime, timedelta

from app.modules.payments.domain.entities import Payment
from app.modules.payments.domain.enums import PaymentMethodType
from app.modules.payments.domain.money import Money

T0 = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)


def make_payment(
    route: list[str] | None = None, *, budget: timedelta = timedelta(seconds=15)
) -> Payment:
    return Payment.create(
        customer_id="cust_1",
        idempotency_key="key-123456",
        request_hash="h",
        money=Money(amount_minor=1999, currency="USD"),
        country="DE",
        method=PaymentMethodType.CARD,
        payment_token="tok_approve",
        description=None,
        route=route if route is not None else ["a", "b", "c"],
        now=T0,
        time_budget=budget,
    )


def processing_payment(route: list[str] | None = None, **kwargs) -> Payment:  # type: ignore[no-untyped-def]
    payment = make_payment(route, **kwargs)
    payment.start_processing(T0)
    return payment
