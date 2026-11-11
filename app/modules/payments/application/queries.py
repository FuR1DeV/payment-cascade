from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from app.core.clock import Clock, utc_now
from app.modules.payments.domain.entities import Alert, Payment

from .ports import CircuitBreaker, ProviderRegistry, UnitOfWorkFactory


@dataclass(frozen=True)
class ProviderStats:
    provider: str
    attempts: int
    approved: int
    declined_soft: int
    declined_hard: int
    errors: int
    unknown: int
    approval_rate: float | None  # approved / (approved + declines): business answers only
    technical_failure_rate: float | None  # (errors + unknown) / attempts
    circuit: dict[str, Any]


class PaymentQueries:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        providers: ProviderRegistry,
        breaker: CircuitBreaker,
        clock: Clock = utc_now,
    ) -> None:
        self._uow_factory = uow_factory
        self._providers = providers
        self._breaker = breaker
        self._clock = clock

    async def get_payment(self, payment_id: UUID) -> Payment | None:
        async with self._uow_factory() as uow:
            return await uow.payments.get(payment_id)

    async def list_alerts(self, payment_id: UUID | None, limit: int = 50) -> list[Alert]:
        async with self._uow_factory() as uow:
            return await uow.alerts.list(payment_id, limit)

    async def provider_stats(self, window: timedelta) -> list[ProviderStats]:
        async with self._uow_factory() as uow:
            rows = {
                r["provider"]: r for r in await uow.payments.attempt_stats(self._clock() - window)
            }
        result = []
        for code in sorted(set(self._providers.codes()) | set(rows)):
            r = rows.get(code, {})
            approved = int(r.get("approved", 0))
            soft = int(r.get("declined_soft", 0))
            hard = int(r.get("declined_hard", 0))
            errors = int(r.get("errors", 0))
            unknown = int(r.get("unknown", 0))
            attempts = int(r.get("attempts", 0))
            answered = approved + soft + hard
            result.append(
                ProviderStats(
                    provider=code,
                    attempts=attempts,
                    approved=approved,
                    declined_soft=soft,
                    declined_hard=hard,
                    errors=errors,
                    unknown=unknown,
                    approval_rate=round(approved / answered, 4) if answered else None,
                    technical_failure_rate=(
                        round((errors + unknown) / attempts, 4) if attempts else None
                    ),
                    circuit=await self._breaker.state(code),
                )
            )
        return result
