from datetime import datetime
from types import TracebackType
from typing import Any, Self
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.payments.application.ports import AttemptRef
from app.modules.payments.domain.entities import Alert, Payment, PaymentAttempt
from app.modules.payments.domain.enums import AttemptStatus, PaymentStatus
from app.modules.payments.domain.money import Money
from app.modules.payments.domain.routing import RoutingRule

from .tables import payment_alerts, payment_attempts, payments, routing_rules, webhook_events

_ATTEMPT_IMMUTABLE = {"id", "payment_id", "attempt_no", "provider", "idempotency_key", "created_at"}


def _payment_values(p: Payment) -> dict[str, Any]:
    return {
        "id": p.id,
        "customer_id": p.customer_id,
        "idempotency_key": p.idempotency_key,
        "request_hash": p.request_hash,
        "amount_minor": p.money.amount_minor,
        "currency": p.money.currency,
        "country": p.country,
        "method": p.method.value,
        "payment_token": p.payment_token,
        "description": p.description,
        "status": p.status.value,
        "route": p.route,
        "succeeded_attempt_no": p.succeeded_attempt_no,
        "failure_reason": p.failure_reason,
        "review_reason": p.review_reason,
        "created_at": p.created_at,
        "updated_at": p.updated_at,
        "deadline_at": p.deadline_at,
    }


def _attempt_values(a: PaymentAttempt) -> dict[str, Any]:
    values = a.model_dump()
    values["status"] = a.status.value
    return values


def _to_payment(row: sa.RowMapping, attempt_rows: list[sa.RowMapping]) -> Payment:
    data = dict(row)
    money = Money(amount_minor=data.pop("amount_minor"), currency=data.pop("currency"))
    attempts = [PaymentAttempt.model_validate(dict(a)) for a in attempt_rows]
    return Payment.model_validate({**data, "money": money, "attempts": attempts})


class SqlPaymentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def insert_if_absent(self, payment: Payment) -> bool:
        stmt = (
            pg_insert(payments)
            .values(**_payment_values(payment))
            .on_conflict_do_nothing(constraint="uq_payments_idempotency")
            .returning(payments.c.id)
        )
        inserted = (await self._s.execute(stmt)).scalar_one_or_none() is not None
        if inserted:
            for attempt in payment.attempts:
                await self._upsert_attempt(attempt)
        return inserted

    async def get(self, payment_id: UUID, *, for_update: bool = False) -> Payment | None:
        stmt = sa.select(payments).where(payments.c.id == payment_id)
        if for_update:
            stmt = stmt.with_for_update()
        row = (await self._s.execute(stmt)).mappings().one_or_none()
        if row is None:
            return None
        return await self._load(row)

    async def get_by_idempotency_key(self, customer_id: str, key: str) -> Payment | None:
        stmt = sa.select(payments).where(
            payments.c.customer_id == customer_id, payments.c.idempotency_key == key
        )
        row = (await self._s.execute(stmt)).mappings().one_or_none()
        if row is None:
            return None
        return await self._load(row)

    async def _load(self, row: sa.RowMapping) -> Payment:
        attempts = (
            (
                await self._s.execute(
                    sa.select(payment_attempts)
                    .where(payment_attempts.c.payment_id == row["id"])
                    .order_by(payment_attempts.c.attempt_no)
                )
            )
            .mappings()
            .all()
        )
        return _to_payment(row, list(attempts))

    async def save(self, payment: Payment) -> None:
        values = _payment_values(payment)
        del values["id"]
        await self._s.execute(
            sa.update(payments).where(payments.c.id == payment.id).values(**values)
        )
        for attempt in payment.attempts:
            await self._upsert_attempt(attempt)

    async def _upsert_attempt(self, attempt: PaymentAttempt) -> None:
        values = _attempt_values(attempt)
        stmt = pg_insert(payment_attempts).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[payment_attempts.c.id],
            set_={k: stmt.excluded[k] for k in values if k not in _ATTEMPT_IMMUTABLE},
        )
        await self._s.execute(stmt)

    async def find_payment_id_by_attempt(
        self, provider: str, *, attempt_key: str | None, provider_payment_id: str | None
    ) -> UUID | None:
        conditions = []
        if attempt_key:
            conditions.append(payment_attempts.c.idempotency_key == attempt_key)
        if provider_payment_id:
            conditions.append(payment_attempts.c.provider_payment_id == provider_payment_id)
        if not conditions:
            return None
        stmt = (
            sa.select(payment_attempts.c.payment_id)
            .where(payment_attempts.c.provider == provider, sa.or_(*conditions))
            .limit(1)
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def claim_due_status_checks(
        self, now: datetime, lease_until: datetime, limit: int
    ) -> list[AttemptRef]:
        due = (
            sa.select(payment_attempts.c.id)
            .join(payments, payments.c.id == payment_attempts.c.payment_id)
            .where(
                payment_attempts.c.status == AttemptStatus.UNKNOWN.value,
                payments.c.status == PaymentStatus.REQUIRES_REVIEW.value,
                sa.or_(
                    payment_attempts.c.next_status_check_at.is_(None),
                    payment_attempts.c.next_status_check_at <= now,
                ),
            )
            .order_by(payment_attempts.c.next_status_check_at)
            .limit(limit)
            .with_for_update(of=payment_attempts, skip_locked=True)
        )
        return await self._claim(due, payment_attempts.c.next_status_check_at, lease_until)

    async def claim_due_refunds(
        self, now: datetime, lease_until: datetime, limit: int
    ) -> list[AttemptRef]:
        due = (
            sa.select(payment_attempts.c.id)
            .where(
                payment_attempts.c.status == AttemptStatus.REFUND_PENDING.value,
                sa.or_(
                    payment_attempts.c.next_refund_at.is_(None),
                    payment_attempts.c.next_refund_at <= now,
                ),
            )
            .order_by(payment_attempts.c.next_refund_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return await self._claim(due, payment_attempts.c.next_refund_at, lease_until)

    async def _claim(
        self, due: sa.Select[Any], column: sa.Column[Any], lease_until: datetime
    ) -> list[AttemptRef]:
        stmt = (
            sa.update(payment_attempts)
            .where(payment_attempts.c.id.in_(due.scalar_subquery()))
            .values({column.name: lease_until})
            .returning(payment_attempts.c.payment_id, payment_attempts.c.attempt_no)
        )
        rows = (await self._s.execute(stmt)).all()
        return [AttemptRef(payment_id=r.payment_id, attempt_no=r.attempt_no) for r in rows]

    async def find_stale(self, older_than: datetime, limit: int) -> list[UUID]:
        stmt = (
            sa.select(payments.c.id)
            .where(
                payments.c.status.in_(
                    [PaymentStatus.CREATED.value, PaymentStatus.PROCESSING.value]
                ),
                payments.c.deadline_at < older_than,
            )
            .order_by(payments.c.deadline_at)
            .limit(limit)
        )
        return list((await self._s.execute(stmt)).scalars().all())

    async def attempt_stats(self, since: datetime) -> list[dict[str, Any]]:
        status = payment_attempts.c.status

        def count_in(*values: AttemptStatus) -> sa.ColumnElement[int]:
            return sa.func.count().filter(status.in_([v.value for v in values]))

        stmt = (
            sa.select(
                payment_attempts.c.provider.label("provider"),
                sa.func.count().label("attempts"),
                count_in(
                    AttemptStatus.APPROVED, AttemptStatus.REFUND_PENDING, AttemptStatus.REFUNDED
                ).label("approved"),
                count_in(AttemptStatus.DECLINED_SOFT).label("declined_soft"),
                count_in(AttemptStatus.DECLINED_HARD).label("declined_hard"),
                count_in(AttemptStatus.ERROR).label("errors"),
                count_in(AttemptStatus.UNKNOWN, AttemptStatus.PENDING).label("unknown"),
            )
            .where(payment_attempts.c.created_at >= since)
            .group_by(payment_attempts.c.provider)
        )
        return [dict(r) for r in (await self._s.execute(stmt)).mappings().all()]


class SqlWebhookJournal:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def append(
        self, provider: str, event_id: str, event_type: str, payload: dict[str, Any]
    ) -> bool:
        stmt = (
            pg_insert(webhook_events)
            .values(provider=provider, event_id=event_id, event_type=event_type, payload=payload)
            .on_conflict_do_nothing(constraint="uq_webhook_events_provider_event")
            .returning(webhook_events.c.id)
        )
        return (await self._s.execute(stmt)).scalar_one_or_none() is not None


class SqlAlertRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, alert: Alert) -> None:
        values = alert.model_dump(mode="json")
        values["id"] = alert.id
        values["payment_id"] = alert.payment_id
        values["created_at"] = alert.created_at
        await self._s.execute(sa.insert(payment_alerts).values(**values))

    async def list(self, payment_id: UUID | None, limit: int) -> list[Alert]:
        stmt = sa.select(payment_alerts).order_by(payment_alerts.c.created_at.desc()).limit(limit)
        if payment_id is not None:
            stmt = stmt.where(payment_alerts.c.payment_id == payment_id)
        rows = (await self._s.execute(stmt)).mappings().all()
        return [Alert.model_validate(dict(r)) for r in rows]


class SqlRoutingRuleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def list_active(self) -> list[RoutingRule]:
        stmt = sa.select(routing_rules).where(routing_rules.c.is_active.is_(True))
        rows = (await self._s.execute(stmt)).mappings().all()
        return [RoutingRule.model_validate(dict(r)) for r in rows]


class SqlAlchemyUnitOfWork:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def __aenter__(self) -> Self:
        self._session = self._session_factory()
        self.payments = SqlPaymentRepository(self._session)
        self.webhooks = SqlWebhookJournal(self._session)
        self.alerts = SqlAlertRepository(self._session)
        self.routing_rules = SqlRoutingRuleRepository(self._session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Anything not committed explicitly is rolled back; this also releases row locks.
        await self._session.rollback()
        await self._session.close()

    async def commit(self) -> None:
        await self._session.commit()
