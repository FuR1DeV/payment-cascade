from pydantic import BaseModel, ConfigDict

from .enums import OutcomeKind


class ProviderOutcome(BaseModel):
    """What a provider told us about one attempt, already normalized by the adapter."""

    model_config = ConfigDict(frozen=True)

    kind: OutcomeKind
    provider_payment_id: str | None = None
    code: str | None = None  # raw provider code, e.g. "05" or "stolen_card"
    message: str | None = None

    @classmethod
    def approved(
        cls, provider_payment_id: str | None = None, code: str | None = None
    ) -> "ProviderOutcome":
        return cls(kind=OutcomeKind.APPROVED, provider_payment_id=provider_payment_id, code=code)

    @classmethod
    def unknown(cls, code: str, message: str | None = None) -> "ProviderOutcome":
        return cls(kind=OutcomeKind.UNKNOWN, code=code, message=message)

    @classmethod
    def technical_error(cls, code: str, message: str | None = None) -> "ProviderOutcome":
        return cls(kind=OutcomeKind.TECHNICAL_ERROR, code=code, message=message)
