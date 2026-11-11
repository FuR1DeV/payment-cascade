import httpx

from app.core.config import Settings
from app.modules.payments.application.ports import PaymentProvider

from .mock_psp import AlphaProvider, BetaProvider
from .stripe import StripeProvider
from .vault import TestTokenVault


def build_providers(settings: Settings) -> tuple[list[PaymentProvider], list[httpx.AsyncClient]]:
    """One pooled httpx client per provider. Returns the clients so they can be closed."""
    vault = TestTokenVault()
    clients: list[httpx.AsyncClient] = []

    def client(base_url: str, timeout: float, **kwargs: object) -> httpx.AsyncClient:
        c = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 2.0)),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
            **kwargs,  # type: ignore[arg-type]
        )
        clients.append(c)
        return c

    mock_timeout = settings.mock_provider_timeout_seconds
    base = settings.mock_psp_base_url.rstrip("/")
    providers: list[PaymentProvider] = [
        AlphaProvider(
            code="mock_alpha",
            client=client(f"{base}/alpha", mock_timeout),
            webhook_secret=settings.mock_alpha_webhook_secret.get_secret_value(),
            timeout_seconds=mock_timeout,
            vault=vault,
        ),
        BetaProvider(
            code="mock_beta",
            client=client(f"{base}/beta", mock_timeout),
            webhook_secret=settings.mock_beta_webhook_secret.get_secret_value(),
            timeout_seconds=mock_timeout,
            vault=vault,
        ),
        BetaProvider(
            code="mock_gamma",
            client=client(f"{base}/gamma", mock_timeout),
            webhook_secret=settings.mock_gamma_webhook_secret.get_secret_value(),
            timeout_seconds=mock_timeout,
            vault=vault,
            currencies=frozenset({"USD", "EUR"}),
        ),
    ]
    if settings.stripe_secret_key is not None:
        providers.append(
            StripeProvider(
                client=client(
                    settings.stripe_api_base_url,
                    settings.stripe_timeout_seconds,
                    headers={
                        "Authorization": f"Bearer {settings.stripe_secret_key.get_secret_value()}"
                    },
                ),
                webhook_secret=(
                    settings.stripe_webhook_secret.get_secret_value()
                    if settings.stripe_webhook_secret
                    else None
                ),
                timeout_seconds=settings.stripe_timeout_seconds,
                vault=vault,
            )
        )
    return providers, clients
