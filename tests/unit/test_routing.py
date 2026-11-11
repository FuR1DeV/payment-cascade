from app.modules.payments.domain.enums import PaymentMethodType
from app.modules.payments.domain.money import Money
from app.modules.payments.domain.routing import RoutingRule, select_route

CARD = PaymentMethodType.CARD
RULES = [
    RoutingRule(id=1, priority=100, providers=["alpha", "beta", "gamma"]),
    RoutingRule(id=2, priority=10, country="US", providers=["stripe", "alpha"]),
    RoutingRule(id=3, priority=20, currency="EUR", providers=["beta", "alpha", "beta"]),
    RoutingRule(id=4, priority=5, min_amount_minor=100_000, providers=["gamma"]),
]


def usd(amount: int = 1999) -> Money:
    return Money(amount_minor=amount, currency="USD")


def test_default_rule() -> None:
    assert select_route(RULES, usd(), "DE", CARD) == ["alpha", "beta", "gamma"]


def test_country_rule_has_priority() -> None:
    assert select_route(RULES, usd(), "US", CARD) == ["stripe", "alpha"]


def test_currency_rule_and_duplicates_removed() -> None:
    eur = Money(amount_minor=500, currency="EUR")
    assert select_route(RULES, eur, "FR", CARD) == ["beta", "alpha"]


def test_amount_rule() -> None:
    assert select_route(RULES, usd(250_000), "US", CARD) == ["gamma"]


def test_no_rule_no_route() -> None:
    assert select_route([], usd(), "DE", CARD) == []
