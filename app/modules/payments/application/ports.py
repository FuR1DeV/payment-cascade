"""Ports: what the application layer needs from the outside world."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from types import TracebackType
from typing import Any, Protocol, Self
from uuid import UUID

from app.modules.payments.domain.entities import Alert, Payment
from app.modules.payments.domain.money import Money
from app.modules.payments.domain.outcome import ProviderOutcome
from app.modules.payments.domain.routing import RoutingRule

# --- providers ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChargeRequest:
    payment_id: UUID
    attempt_no: int
    idempotency_key: str  # payment_id:attempt_no, the provider must dedupe on it
    money: Money
    payment_token: str  # opaque token from our (test) vault, never a card number
    country: str
    description: str | None = None


@dataclass(frozen=True)
class StatusRequest:
    charge: ChargeRequest
    provider_payment_id: str | None = None
    attempt_age: timedelta | None = None  # time since the attempt was created
    # A provider may answer a status query by repeating the request with the same
    # idempotency key (Stripe). That is a write: if the first request never arrived, the
    # repeat is the charge. Allowed only while the cascade runs and the client waits.
    replay_allowed: bool = False


@dataclass(frozen=True)
class RefundRequest:
    idempotency_key: str
    attempt_key: str
    provider_payment_id: str | None
    money: Money


@dataclass(frozen=True)
class RefundResult:
    succeeded: bool
    refund_id: str | None = None
    error: str | None = None


class WebhookKind(StrEnum):
    PAYMENT_OUTCOME = "payment_outcome"
    IGNORED = "ignored"  # event types we do not act on (refund notices, etc.)


@dataclass(frozen=True)
class WebhookEvent:
    event_id: str
    event_type: str
    kind: WebhookKind
    attempt_key: str | None = None
    provider_payment_id: str | None = None
    outcome: ProviderOutcome | None = None
    payload: dict[str, Any] = field(default_factory=dict)


class InvalidWebhookError(Exception):
    """Bad signature, stale timestamp or malformed body."""


class PaymentProvider(Protocol):
    """Adapter contract. charge/get_status never raise on network problems: they return
    an outcome. TECHNICAL_ERROR only when the request surely was not processed."""

    code: str

    def supports(self, money: Money, country: str, payment_token: str) -> bool: ...

    async def charge(self, request: ChargeRequest) -> ProviderOutcome: ...

    async def get_status(self, request: StatusRequest) -> ProviderOutcome: ...

    async def refund(self, request: RefundRequest) -> RefundResult: ...

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> WebhookEvent: ...


class UnknownProviderError(KeyError):
    pass


class ProviderRegistry:
    def __init__(self, providers: list[PaymentProvider]) -> None:
        self._providers = {p.code: p for p in providers}

    def get(self, code: str) -> PaymentProvider:
        try:
            return self._providers[code]
        except KeyError:
            raise UnknownProviderError(code) from None

    def __contains__(self, code: object) -> bool:
        return code in self._providers

    def codes(self) -> list[str]:
        return list(self._providers)


# --- infrastructure services -------------------------------------------------------------


class CircuitBreaker(Protocol):
    async def is_available(self, provider: str) -> bool: ...

    async def record(self, provider: str, *, success: bool) -> None: ...

    async def state(self, provider: str) -> dict[str, Any]: ...


class JobQueue(Protocol):
    async def kick_refunds(self) -> None: ...


# --- persistence -------------------------------------------------------------------------


@dataclass(frozen=True)
class AttemptRef:
    payment_id: UUID
    attempt_no: int


class PaymentRepository(Protocol):
    async def insert_if_absent(self, payment: Payment) -> bool: ...

    async def get(self, payment_id: UUID, *, for_update: bool = False) -> Payment | None: ...

    async def get_by_idempotency_key(self, customer_id: str, key: str) -> Payment | None: ...

    async def save(self, payment: Payment) -> None: ...

    async def find_payment_id_by_attempt(
        self, provider: str, *, attempt_key: str | None, provider_payment_id: str | None
    ) -> UUID | None: ...

    async def claim_due_status_checks(
        self, now: datetime, lease_until: datetime, limit: int
    ) -> list[AttemptRef]: ...

    async def claim_due_refunds(
        self, now: datetime, lease_until: datetime, limit: int
    ) -> list[AttemptRef]: ...

    async def find_stale(self, older_than: datetime, limit: int) -> list[UUID]: ...

    async def attempt_stats(self, since: datetime) -> list[dict[str, Any]]: ...


class WebhookJournal(Protocol):
    async def append(
        self, provider: str, event_id: str, event_type: str, payload: dict[str, Any]
    ) -> bool: ...


class AlertRepository(Protocol):
    async def add(self, alert: Alert) -> None: ...

    async def list(self, payment_id: UUID | None, limit: int) -> list[Alert]: ...


class RoutingRuleRepository(Protocol):
    async def list_active(self) -> list[RoutingRule]: ...


class UnitOfWork(Protocol):
    payments: PaymentRepository
    webhooks: WebhookJournal
    alerts: AlertRepository
    routing_rules: RoutingRuleRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...


type UnitOfWorkFactory = Callable[[], UnitOfWork]
