from functools import lru_cache
from typing import Any

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings. Real secrets come only from the environment / .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    log_level: str = "INFO"
    # Static demo page at /demo/ that drives the scenarios through the public API.
    # For local runs and reviewers only; switch it off anywhere real.
    demo_page_enabled: bool = True

    database_url: str = "postgresql+asyncpg://payments:payments@localhost:5432/payments"
    database_pool_size: int = 10
    redis_url: str = "redis://localhost:6379/0"

    # Cascade limits.
    payment_time_budget_seconds: float = 15.0
    max_attempts: int = 3
    min_attempt_budget_seconds: float = 3.0

    # Inline status checks after a timeout (while the client still waits).
    status_check_initial_delay_seconds: float = 0.5
    status_check_max_delay_seconds: float = 2.0

    # Background work (Taskiq).
    reconcile_backoff_base_seconds: float = 10.0
    reconcile_backoff_max_seconds: float = 600.0
    reconcile_alert_after_checks: int = 30
    refund_backoff_base_seconds: float = 5.0
    refund_backoff_max_seconds: float = 300.0
    refund_alert_after_attempts: int = 5
    stale_payment_grace_seconds: float = 30.0
    claim_lease_seconds: float = 60.0

    # Circuit breaker (per provider, in Redis).
    circuit_window_seconds: int = 60
    circuit_bucket_seconds: int = 10
    circuit_min_requests: int = 10
    circuit_failure_rate: float = 0.5
    circuit_open_seconds: int = 30

    # Mock providers (run in the mock-psp container). Their secrets are dev-only values.
    mock_psp_base_url: str = "http://localhost:9000"
    mock_provider_timeout_seconds: float = 3.0
    mock_alpha_webhook_secret: SecretStr = SecretStr("dev-alpha-secret")
    mock_beta_webhook_secret: SecretStr = SecretStr("dev-beta-secret")
    mock_gamma_webhook_secret: SecretStr = SecretStr("dev-gamma-secret")

    # Stripe in test mode. Optional: without a key the provider is simply not registered.
    stripe_secret_key: SecretStr | None = None
    stripe_webhook_secret: SecretStr | None = None
    stripe_api_base_url: str = "https://api.stripe.com"
    stripe_timeout_seconds: float = 10.0

    @field_validator("stripe_secret_key", "stripe_webhook_secret", mode="before")
    @classmethod
    def _empty_is_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("stripe_secret_key")
    @classmethod
    def _only_test_mode(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value().startswith(("sk_test_", "rk_test_")):
            raise ValueError("only Stripe test-mode keys (sk_test_... / rk_test_...) are accepted")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
