from pydantic import BaseModel, ConfigDict, Field


class Money(BaseModel):
    """Amount in minor units (cents) next to its currency. Floats are rejected on purpose."""

    model_config = ConfigDict(frozen=True)

    amount_minor: int = Field(strict=True, gt=0, le=10**12)
    currency: str = Field(pattern=r"^[A-Z]{3}$")

    def __str__(self) -> str:
        return f"{self.amount_minor} {self.currency} (minor units)"
