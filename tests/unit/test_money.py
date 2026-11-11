import pytest
from pydantic import ValidationError

from app.modules.payments.domain.money import Money


def test_amount_is_integer_minor_units() -> None:
    assert Money(amount_minor=1999, currency="USD").amount_minor == 1999


@pytest.mark.parametrize("amount", [19.99, 19.0, "1999", True, 0, -1])
def test_rejects_floats_strings_and_non_positive(amount: object) -> None:
    with pytest.raises(ValidationError):
        Money(amount_minor=amount, currency="USD")  # type: ignore[arg-type]


@pytest.mark.parametrize("currency", ["usd", "US", "USDT", ""])
def test_rejects_bad_currency(currency: str) -> None:
    with pytest.raises(ValidationError):
        Money(amount_minor=100, currency=currency)


def test_is_immutable() -> None:
    money = Money(amount_minor=100, currency="EUR")
    with pytest.raises(ValidationError):
        money.amount_minor = 200  # type: ignore[misc]
