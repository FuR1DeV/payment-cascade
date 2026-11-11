import logging
from collections.abc import Mapping
from enum import StrEnum

from app.core.clock import Clock, utc_now
from app.core.logging import log_context

from .common import persist
from .ports import JobQueue, ProviderRegistry, UnitOfWorkFactory, WebhookKind

logger = logging.getLogger("payments.webhooks")


class WebhookResult(StrEnum):
    APPLIED = "applied"
    NO_CHANGE = "no_change"
    DUPLICATE = "duplicate"
    IGNORED = "ignored"
    UNKNOWN_PAYMENT = "unknown_payment"


class HandleWebhook:
    """Verify, journal and apply a provider webhook, all in one database transaction."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        providers: ProviderRegistry,
        jobs: JobQueue,
        clock: Clock = utc_now,
    ) -> None:
        self._uow_factory = uow_factory
        self._providers = providers
        self._jobs = jobs
        self._clock = clock

    async def __call__(
        self, provider_code: str, headers: Mapping[str, str], body: bytes
    ) -> WebhookResult:
        provider = self._providers.get(provider_code)  # UnknownProviderError -> 404
        event = provider.parse_webhook(headers, body)  # InvalidWebhookError -> 400

        with log_context(provider=provider_code, event_id=event.event_id):
            async with self._uow_factory() as uow:
                # Append-only journal, unique (provider, event_id). A concurrent duplicate
                # blocks on the unique index until we commit, then inserts nothing.
                if not await uow.webhooks.append(
                    provider_code, event.event_id, event.event_type, event.payload
                ):
                    logger.info("webhook_duplicate")
                    return WebhookResult.DUPLICATE

                if event.kind is WebhookKind.IGNORED:
                    await uow.commit()
                    return WebhookResult.IGNORED

                payment_id = await uow.payments.find_payment_id_by_attempt(
                    provider_code,
                    attempt_key=event.attempt_key,
                    provider_payment_id=event.provider_payment_id,
                )
                if payment_id is None:
                    logger.warning("webhook_for_unknown_payment")
                    await uow.commit()  # keep the raw event for investigation
                    return WebhookResult.UNKNOWN_PAYMENT

                payment = await uow.payments.get(payment_id, for_update=True)
                assert payment is not None  # noqa: S101 - found by id a moment ago
                attempt = payment.find_attempt(
                    attempt_key=event.attempt_key, provider_payment_id=event.provider_payment_id
                )
                # The attempt must belong to this provider: one PSP cannot settle another's.
                if attempt is None or attempt.provider != provider_code:
                    logger.warning(
                        "webhook_attempt_mismatch", extra={"payment_id": str(payment_id)}
                    )
                    await uow.commit()
                    return WebhookResult.UNKNOWN_PAYMENT

                now = self._clock()
                with log_context(payment_id=str(payment.id), attempt_no=attempt.attempt_no):
                    changed = event.outcome is not None and payment.record_outcome(
                        attempt.attempt_no, event.outcome, now
                    )
                    await persist(uow, payment, self._jobs)
                    logger.info("webhook_processed", extra={"changed": changed})
                return WebhookResult.APPLIED if changed else WebhookResult.NO_CHANGE
