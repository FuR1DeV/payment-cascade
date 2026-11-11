"""Stand-in for a card vault.

Cascading one card across several providers needs the card data (or a network token)
to be usable at each of them. In production this is a PCI DSS compliant vault or
network tokens; adapters would get a provider-specific credential from it. Here the
client sends an opaque test token, and this class maps it to what each test provider
accepts. No card numbers are stored or accepted anywhere in the service.
"""

# Stripe test-mode PaymentMethods. See https://docs.stripe.com/testing
STRIPE_TEST_PAYMENT_METHODS: dict[str, str] = {
    "tok_approve": "pm_card_visa",
    "tok_soft_decline": "pm_card_chargeDeclined",  # generic_decline
    "tok_all_soft": "pm_card_chargeDeclined",
    "tok_hard_decline": "pm_card_chargeDeclinedStolenCard",  # stolen_card
    "tok_insufficient_funds": "pm_card_chargeDeclinedInsufficientFunds",
    "tok_fraud": "pm_card_chargeDeclinedFraudulent",
}


class TestTokenVault:
    __test__ = False  # not a pytest test class

    def __init__(self, stripe_map: dict[str, str] | None = None) -> None:
        self._stripe = stripe_map if stripe_map is not None else STRIPE_TEST_PAYMENT_METHODS

    def token_for(self, provider: str, token: str) -> str | None:
        if provider.startswith("mock_"):
            # Mock providers understand our test tokens directly (they encode scenarios).
            return token
        if provider == "stripe":
            return self._stripe.get(token)
        return None
