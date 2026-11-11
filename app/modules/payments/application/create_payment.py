import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta

from app.core.clock import Clock, utc_now
from app.modules.payments.domain.entities import Payment
from app.modules.payments.domain.enums import PaymentMethodType
from app.modules.payments.domain.money import Money
from app.modules.payments.domain.routing import select_route

from .ports import ProviderRegistry, UnitOfWorkFactory


@dataclass(frozen=True)
class CreatePaymentCommand:
    customer_id: str
    idempotency_key: str
    money: Money
    country: str
    method: PaymentMethodType
    payment_token: str
    description: str | None = None

    def fingerprint(self) -> str:
        body = {
            "customer_id": self.customer_id,
            "amount_minor": self.money.amount_minor,
            "currency": self.money.currency,
            "country": self.country,
            "method": self.method.value,
            "payment_token": self.payment_token,
            "description": self.description,
        }
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class CreatePaymentResult:
    payment: Payment
    created: bool  # False: the key was seen before, this is the original payment


class IdempotencyKeyReusedError(Exception):
    """Same Idempotency-Key with a different request body."""


class CreatePayment:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        providers: ProviderRegistry,
        time_budget: timedelta,
        clock: Clock = utc_now,
    ) -> None:
        self._uow_factory = uow_factory
        self._providers = providers
        self._time_budget = time_budget
        self._clock = clock

    async def __call__(self, cmd: CreatePaymentCommand) -> CreatePaymentResult:
        fingerprint = cmd.fingerprint()
        async with self._uow_factory() as uow:
            existing = await uow.payments.get_by_idempotency_key(
                cmd.customer_id, cmd.idempotency_key
            )
            if existing is not None:
                return self._replay(existing, fingerprint)

            rules = await uow.routing_rules.list_active()
            route = [
                code
                for code in select_route(rules, cmd.money, cmd.country, cmd.method)
                if code in self._providers
                and self._providers.get(code).supports(cmd.money, cmd.country, cmd.payment_token)
            ]
            payment = Payment.create(
                customer_id=cmd.customer_id,
                idempotency_key=cmd.idempotency_key,
                request_hash=fingerprint,
                money=cmd.money,
                country=cmd.country,
                method=cmd.method,
                payment_token=cmd.payment_token,
                description=cmd.description,
                route=route,
                now=self._clock(),
                time_budget=self._time_budget,
            )
            # Unique (customer_id, idempotency_key): a concurrent twin waits here for our
            # commit and then gets "nothing inserted".
            if await uow.payments.insert_if_absent(payment):
                await uow.commit()
                return CreatePaymentResult(payment=payment, created=True)

            existing = await uow.payments.get_by_idempotency_key(
                cmd.customer_id, cmd.idempotency_key
            )
            if existing is None:  # pragma: no cover - the row we conflicted with must exist
                raise RuntimeError("idempotency conflict without an existing payment")
            return self._replay(existing, fingerprint)

    @staticmethod
    def _replay(existing: Payment, fingerprint: str) -> CreatePaymentResult:
        if existing.request_hash != fingerprint:
            raise IdempotencyKeyReusedError(existing.idempotency_key)
        return CreatePaymentResult(payment=existing, created=False)
