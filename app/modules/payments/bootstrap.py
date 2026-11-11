"""Composition root of the payments module: wires ports to adapters."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.core.db import create_engine, create_session_factory
from app.modules.payments.application.background import (
    ExecutePendingRefunds,
    ReconcileUnknownAttempts,
    RecoverStalePayments,
    RetryPolicy,
)
from app.modules.payments.application.create_payment import CreatePayment
from app.modules.payments.application.ports import (
    CircuitBreaker,
    JobQueue,
    PaymentProvider,
    ProviderRegistry,
    UnitOfWorkFactory,
)
from app.modules.payments.application.process_payment import ProcessPayment, StatusCheckSchedule
from app.modules.payments.application.queries import PaymentQueries
from app.modules.payments.application.webhooks import HandleWebhook
from app.modules.payments.domain.policy import CascadeLimits
from app.modules.payments.infrastructure.circuit_breaker import RedisCircuitBreaker
from app.modules.payments.infrastructure.db.repositories import SqlAlchemyUnitOfWork
from app.modules.payments.infrastructure.jobs import TaskiqJobQueue
from app.modules.payments.infrastructure.providers.factory import build_providers


@dataclass
class Container:
    settings: Settings
    engine: AsyncEngine
    redis: Redis
    providers: ProviderRegistry
    breaker: CircuitBreaker
    jobs: JobQueue
    uow_factory: UnitOfWorkFactory
    create_payment: CreatePayment
    process_payment: ProcessPayment
    handle_webhook: HandleWebhook
    reconcile_unknown: ReconcileUnknownAttempts
    execute_refunds: ExecutePendingRefunds
    recover_stale: RecoverStalePayments
    queries: PaymentQueries
    closers: list[Callable[[], Awaitable[object]]] = field(default_factory=list)

    async def close(self) -> None:
        for close in reversed(self.closers):
            await close()


async def build_container(
    settings: Settings,
    *,
    providers: list[PaymentProvider] | None = None,
    jobs: JobQueue | None = None,
    redis_prefix: str = "cb",
) -> Container:
    closers: list[Callable[[], Awaitable[object]]] = []

    engine = create_engine(settings)
    closers.append(engine.dispose)
    session_factory = create_session_factory(engine)

    def uow_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    redis = Redis.from_url(settings.redis_url, socket_timeout=0.5, socket_connect_timeout=0.5)
    closers.append(redis.aclose)

    if providers is None:
        providers, clients = build_providers(settings)
        closers.extend(c.aclose for c in clients)
    registry = ProviderRegistry(providers)

    breaker = RedisCircuitBreaker(
        redis,
        window_seconds=settings.circuit_window_seconds,
        bucket_seconds=settings.circuit_bucket_seconds,
        min_requests=settings.circuit_min_requests,
        failure_rate=settings.circuit_failure_rate,
        open_seconds=settings.circuit_open_seconds,
        prefix=redis_prefix,
    )
    job_queue: JobQueue = jobs if jobs is not None else TaskiqJobQueue()
    lease = timedelta(seconds=settings.claim_lease_seconds)

    process_payment = ProcessPayment(
        uow_factory=uow_factory,
        providers=registry,
        breaker=breaker,
        jobs=job_queue,
        limits=CascadeLimits(
            max_attempts=settings.max_attempts,
            min_attempt_budget=timedelta(seconds=settings.min_attempt_budget_seconds),
        ),
        status_checks=StatusCheckSchedule(
            initial_delay=timedelta(seconds=settings.status_check_initial_delay_seconds),
            max_delay=timedelta(seconds=settings.status_check_max_delay_seconds),
        ),
    )
    return Container(
        settings=settings,
        engine=engine,
        redis=redis,
        providers=registry,
        breaker=breaker,
        jobs=job_queue,
        uow_factory=uow_factory,
        create_payment=CreatePayment(
            uow_factory=uow_factory,
            providers=registry,
            time_budget=timedelta(seconds=settings.payment_time_budget_seconds),
        ),
        process_payment=process_payment,
        handle_webhook=HandleWebhook(uow_factory=uow_factory, providers=registry, jobs=job_queue),
        reconcile_unknown=ReconcileUnknownAttempts(
            uow_factory=uow_factory,
            providers=registry,
            jobs=job_queue,
            retry=RetryPolicy(
                base_seconds=settings.reconcile_backoff_base_seconds,
                max_seconds=settings.reconcile_backoff_max_seconds,
                alert_after=settings.reconcile_alert_after_checks,
            ),
            lease=lease,
        ),
        execute_refunds=ExecutePendingRefunds(
            uow_factory=uow_factory,
            providers=registry,
            retry=RetryPolicy(
                base_seconds=settings.refund_backoff_base_seconds,
                max_seconds=settings.refund_backoff_max_seconds,
                alert_after=settings.refund_alert_after_attempts,
            ),
            lease=lease,
        ),
        recover_stale=RecoverStalePayments(
            uow_factory=uow_factory,
            process_payment=process_payment,
            jobs=job_queue,
            grace=timedelta(seconds=settings.stale_payment_grace_seconds),
        ),
        queries=PaymentQueries(uow_factory=uow_factory, providers=registry, breaker=breaker),
        closers=closers,
    )
