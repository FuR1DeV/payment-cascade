"""Routing rules: which providers to try, in which order. Rules live in the database."""

from pydantic import BaseModel, ConfigDict, Field

from .enums import PaymentMethodType
from .money import Money


class RoutingRule(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    priority: int  # lower value is checked first
    currency: str | None = None  # None means "any"
    country: str | None = None
    method: PaymentMethodType | None = None
    min_amount_minor: int | None = None
    max_amount_minor: int | None = None
    providers: list[str] = Field(min_length=1)
    description: str | None = None

    def matches(self, money: Money, country: str, method: PaymentMethodType) -> bool:
        if self.currency is not None and self.currency != money.currency:
            return False
        if self.country is not None and self.country != country:
            return False
        if self.method is not None and self.method != method:
            return False
        if self.min_amount_minor is not None and money.amount_minor < self.min_amount_minor:
            return False
        return not (
            self.max_amount_minor is not None and money.amount_minor > self.max_amount_minor
        )


def select_route(
    rules: list[RoutingRule], money: Money, country: str, method: PaymentMethodType
) -> list[str]:
    """Providers of the first matching rule, duplicates removed, order kept."""
    for rule in sorted(rules, key=lambda r: (r.priority, r.id)):
        if rule.matches(money, country, method):
            return list(dict.fromkeys(rule.providers))
    return []
