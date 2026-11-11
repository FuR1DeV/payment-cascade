"""Stripe (test mode only) through its REST API with httpx.

Status after a timeout: Stripe stores the result of a request under its Idempotency-Key
(for 24 hours), so the safe way to learn what happened is to repeat the same request with
the same key. It either returns the stored result or, if the first request never arrived,
executes it once. If we already know the PaymentIntent id we just read it.
"""

import hmac
import json
import time
from collections.abc import Mapping
from typing import Any

import httpx

from app.modules.payments.application.ports import (
    ChargeRequest,
    InvalidWebhookError,
    RefundRequest,
    RefundResult,
    StatusRequest,
    WebhookEvent,
    WebhookKind,
)
from app.modules.payments.domain.enums import OutcomeKind
from app.modules.payments.domain.money import Money
from app.modules.payments.domain.outcome import ProviderOutcome

from .base import hmac_sha256_hex, safe_json, send
from .mock_psp import classify_decline
from .vault import TestTokenVault

K = OutcomeKind

# decline_code (or error code) -> category. https://docs.stripe.com/declines/codes
STRIPE_CODES: dict[str, OutcomeKind] = {
    "generic_decline": K.SOFT_DECLINE,
    "do_not_honor": K.SOFT_DECLINE,
    "try_again_later": K.SOFT_DECLINE,
    "processing_error": K.SOFT_DECLINE,
    "issuer_not_available": K.SOFT_DECLINE,
    "reenter_transaction": K.SOFT_DECLINE,
    "card_not_supported": K.SOFT_DECLINE,
    "currency_not_supported": K.SOFT_DECLINE,
    "transaction_not_allowed": K.SOFT_DECLINE,
    "insufficient_funds": K.HARD_DECLINE,
    "lost_card": K.HARD_DECLINE,
    "stolen_card": K.HARD_DECLINE,
    "fraudulent": K.HARD_DECLINE,
    "pickup_card": K.HARD_DECLINE,
    "expired_card": K.HARD_DECLINE,
    "incorrect_cvc": K.HARD_DECLINE,
    "incorrect_number": K.HARD_DECLINE,
    "invalid_account": K.HARD_DECLINE,
    "card_velocity_exceeded": K.HARD_DECLINE,
    "restricted_card": K.HARD_DECLINE,
    "security_violation": K.HARD_DECLINE,
    "merchant_blacklist": K.HARD_DECLINE,
    # 3-D Secure flow is out of scope here: stop and let the client handle it.
    "authentication_required": K.HARD_DECLINE,
}


class StripeProvider:
    code = "stripe"
    signature_tolerance_seconds = 300

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        webhook_secret: str | None,
        timeout_seconds: float,
        vault: TestTokenVault,
    ) -> None:
        self._client = client  # base_url and Authorization header are set by the factory
        self._webhook_secret = webhook_secret
        self._timeout = timeout_seconds
        self._vault = vault

    def supports(self, money: Money, country: str, payment_token: str) -> bool:
        return self._vault.token_for(self.code, payment_token) is not None

    def _form(self, request: ChargeRequest) -> dict[str, str]:
        payment_method = self._vault.token_for(self.code, request.payment_token)
        if payment_method is None:  # pragma: no cover - filtered by supports()
            raise ValueError("no Stripe test payment method for this token")
        form = {
            "amount": str(request.money.amount_minor),
            "currency": request.money.currency.lower(),
            "payment_method": payment_method,
            "payment_method_types[]": "card",
            "confirm": "true",
            "metadata[payment_id]": str(request.payment_id),
            "metadata[attempt_key]": request.idempotency_key,
        }
        if request.description:
            form["description"] = request.description
        return form

    async def charge(self, request: ChargeRequest) -> ProviderOutcome:
        response = await send(
            lambda: self._client.post(
                "/v1/payment_intents",
                data=self._form(request),
                headers={"Idempotency-Key": request.idempotency_key},
            ),
            self._timeout,
        )
        if isinstance(response, ProviderOutcome):
            return response
        return self._parse(response)

    async def get_status(self, request: StatusRequest) -> ProviderOutcome:
        if request.provider_payment_id:
            response = await send(
                lambda: self._client.get(f"/v1/payment_intents/{request.provider_payment_id}"),
                self._timeout,
            )
            if isinstance(response, ProviderOutcome):
                return response
            return self._parse(response)
        return await self.charge(request.charge)  # idempotent replay, see module docstring

    def _parse(self, response: httpx.Response) -> ProviderOutcome:
        data = safe_json(response) or {}
        if response.status_code == 200:
            return self._parse_intent(data)
        error = data.get("error") if isinstance(data.get("error"), dict) else {}
        intent_id = (error.get("payment_intent") or {}).get("id")
        if response.status_code == 402:
            code = error.get("decline_code") or error.get("code")
            kind = classify_decline(STRIPE_CODES, self.code, code)
            return ProviderOutcome(
                kind=kind, provider_payment_id=intent_id, code=code, message=error.get("message")
            )
        if response.status_code == 409:
            return ProviderOutcome.unknown("idempotency_key_in_use")
        if response.status_code in (400, 401, 403, 404, 429):
            # Request rejected before any charge (validation, auth, rate limit).
            return ProviderOutcome.technical_error(
                f"http_{response.status_code}", error.get("message")
            )
        return ProviderOutcome.unknown(f"http_{response.status_code}")

    def _parse_intent(self, intent: dict[str, Any]) -> ProviderOutcome:
        status = intent.get("status")
        pid = intent.get("id")
        if status == "succeeded":
            return ProviderOutcome.approved(pid, "succeeded")
        if status == "processing":
            return ProviderOutcome(kind=K.UNKNOWN, provider_payment_id=pid, code="processing")
        if status == "requires_action":
            return ProviderOutcome(
                kind=K.HARD_DECLINE, provider_payment_id=pid, code="authentication_required"
            )
        if status == "requires_payment_method":
            error = intent.get("last_payment_error") or {}
            code = error.get("decline_code") or error.get("code") or "generic_decline"
            kind = classify_decline(STRIPE_CODES, self.code, code)
            return ProviderOutcome(kind=kind, provider_payment_id=pid, code=code)
        if status == "canceled":
            return ProviderOutcome(kind=K.TECHNICAL_ERROR, provider_payment_id=pid, code="canceled")
        return ProviderOutcome(kind=K.UNKNOWN, provider_payment_id=pid, code=str(status))

    async def refund(self, request: RefundRequest) -> RefundResult:
        if not request.provider_payment_id:
            return RefundResult(succeeded=False, error="payment_intent_id_unknown")
        response = await send(
            lambda: self._client.post(
                "/v1/refunds",
                data={"payment_intent": request.provider_payment_id},
                headers={"Idempotency-Key": request.idempotency_key},
            ),
            self._timeout,
        )
        if isinstance(response, ProviderOutcome):
            return RefundResult(succeeded=False, error=response.code)
        data = safe_json(response) or {}
        # "pending" means Stripe accepted the refund and will complete it.
        if response.status_code == 200 and data.get("status") in ("succeeded", "pending"):
            return RefundResult(succeeded=True, refund_id=data.get("id"))
        return RefundResult(succeeded=False, error=f"http_{response.status_code}")

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> WebhookEvent:
        if not self._webhook_secret:
            raise InvalidWebhookError("STRIPE_WEBHOOK_SECRET is not configured")
        items = [
            i.split("=", 1) for i in headers.get("stripe-signature", "").split(",") if "=" in i
        ]
        timestamp = next((v for k, v in items if k == "t"), "")
        signatures = [v for k, v in items if k == "v1"]
        if not timestamp.isdigit() or not signatures:
            raise InvalidWebhookError("missing signature")
        if abs(time.time() - int(timestamp)) > self.signature_tolerance_seconds:
            raise InvalidWebhookError("timestamp outside tolerance")
        expected = hmac_sha256_hex(self._webhook_secret, timestamp.encode() + b"." + body)
        if not any(hmac.compare_digest(expected, s) for s in signatures):
            raise InvalidWebhookError("bad signature")
        try:
            event = json.loads(body)
        except ValueError:
            raise InvalidWebhookError("body is not JSON") from None
        event_id, event_type = str(event.get("id", "")), str(event.get("type", ""))
        intent = (event.get("data") or {}).get("object") or {}
        if not event_id or event_type not in (
            "payment_intent.succeeded",
            "payment_intent.payment_failed",
        ):
            return WebhookEvent(
                event_id=event_id or "unknown",
                event_type=event_type,
                kind=WebhookKind.IGNORED,
                payload=event,
            )
        return WebhookEvent(
            event_id=event_id,
            event_type=event_type,
            kind=WebhookKind.PAYMENT_OUTCOME,
            attempt_key=(intent.get("metadata") or {}).get("attempt_key"),
            provider_payment_id=intent.get("id"),
            outcome=self._parse_intent(intent),
            payload=event,
        )
