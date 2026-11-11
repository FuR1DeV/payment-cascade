"""Adapters for the mock providers served by the mock-psp container.

Two different API "dialects" on purpose, to show that every provider has its own codes
and error semantics and the adapter is where they are normalized:
- alpha: ISO 8583 style response codes ("00", "05", "43"...);
- beta / gamma: named decline reasons ("do_not_honor", "stolen_card"...).
"""

import hmac
import json
import logging
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
from .vault import TestTokenVault

logger = logging.getLogger("payments.providers")

K = OutcomeKind

# ISO 8583 response codes used by "alpha".
ALPHA_CODES: dict[str, OutcomeKind] = {
    "00": K.APPROVED,
    "05": K.SOFT_DECLINE,  # do not honor: often acquirer/risk specific, another route may pass
    "91": K.SOFT_DECLINE,  # issuer or switch inoperative
    "51": K.HARD_DECLINE,  # insufficient funds: same issuer behind every acquirer
    "14": K.HARD_DECLINE,  # invalid card number
    "41": K.HARD_DECLINE,  # lost card
    "43": K.HARD_DECLINE,  # stolen card
    "54": K.HARD_DECLINE,  # expired card
    "59": K.HARD_DECLINE,  # suspected fraud
    "96": K.TECHNICAL_ERROR,  # system malfunction; alpha's contract: request not processed
}

# Named reasons used by "beta" and "gamma".
BETA_CODES: dict[str, OutcomeKind] = {
    "do_not_honor": K.SOFT_DECLINE,
    "generic_decline": K.SOFT_DECLINE,
    "try_again_later": K.SOFT_DECLINE,
    "insufficient_funds": K.HARD_DECLINE,
    "invalid_card": K.HARD_DECLINE,
    "lost_card": K.HARD_DECLINE,
    "stolen_card": K.HARD_DECLINE,
    "expired_card": K.HARD_DECLINE,
    "fraud_suspected": K.HARD_DECLINE,
    "service_unavailable": K.TECHNICAL_ERROR,
}


def classify_decline(table: dict[str, OutcomeKind], provider: str, code: str | None) -> OutcomeKind:
    if code in table:
        return table[code]
    # Unknown decline reason: money was not taken, but we do not know why. Stopping is the
    # conservative choice (no retries on possibly fraudulent cards); the code is logged
    # so it can be added to the table.
    logger.warning("unmapped_decline_code", extra={"provider": provider, "provider_code": code})
    return K.HARD_DECLINE


class _MockProviderBase:
    code: str

    def __init__(
        self,
        *,
        code: str,
        client: httpx.AsyncClient,
        webhook_secret: str,
        timeout_seconds: float,
        vault: TestTokenVault,
        currencies: frozenset[str] | None = None,
    ) -> None:
        self.code = code
        self._client = client
        self._secret = webhook_secret
        self._timeout = timeout_seconds
        self._vault = vault
        self._currencies = currencies

    def supports(self, money: Money, country: str, payment_token: str) -> bool:
        if self._currencies is not None and money.currency not in self._currencies:
            return False
        return self._vault.token_for(self.code, payment_token) is not None

    def _token(self, request: ChargeRequest) -> str:
        token = self._vault.token_for(self.code, request.payment_token)
        if token is None:  # pragma: no cover - filtered by supports() at routing time
            raise ValueError("no token for provider")
        return token


class AlphaProvider(_MockProviderBase):
    """ISO 8583 dialect. Signature: X-Alpha-Signature = hex(HMAC-SHA256(secret, body))."""

    async def charge(self, request: ChargeRequest) -> ProviderOutcome:
        body = {
            "amount": request.money.amount_minor,
            "currency": request.money.currency,
            "card_token": self._token(request),
            "reference": request.idempotency_key,
            "description": request.description,
        }
        response = await send(
            lambda: self._client.post(
                "/payments", json=body, headers={"Idempotency-Key": request.idempotency_key}
            ),
            self._timeout,
        )
        if isinstance(response, ProviderOutcome):
            return response
        return self._parse(response)

    async def get_status(self, request: StatusRequest) -> ProviderOutcome:
        response = await send(
            lambda: self._client.get(
                "/payments", params={"reference": request.charge.idempotency_key}
            ),
            self._timeout,
        )
        if isinstance(response, ProviderOutcome):
            return response
        if response.status_code == 404:
            # Alpha's contract: the reference is stored when the request is received.
            # "Not found" therefore means the request never arrived: nothing was charged.
            return ProviderOutcome.technical_error("not_found", "provider has no such payment")
        return self._parse(response)

    def _parse(self, response: httpx.Response) -> ProviderOutcome:
        data = safe_json(response) or {}
        code = data.get("response_code")
        if response.status_code == 200:
            status = data.get("status")
            pid = data.get("id")
            message = data.get("message")
            if status == "approved" and code == "00":
                return ProviderOutcome.approved(pid, code)
            if status == "processing":
                return ProviderOutcome(kind=K.UNKNOWN, provider_payment_id=pid, code="processing")
            if status in ("declined", "failed"):
                kind = classify_decline(ALPHA_CODES, self.code, code)
                return ProviderOutcome(
                    kind=kind, provider_payment_id=pid, code=code, message=message
                )
            return ProviderOutcome.unknown("unexpected_response", json.dumps(data)[:200])
        if response.status_code == 503 and code == "96" and data.get("processed") is False:
            return ProviderOutcome.technical_error("96", data.get("error"))
        if response.status_code == 409:
            return ProviderOutcome.unknown("request_in_progress")
        if 400 <= response.status_code < 500:
            # Rejected before processing (validation, auth). Not charged.
            return ProviderOutcome.technical_error(
                f"http_{response.status_code}", data.get("error")
            )
        return ProviderOutcome.unknown(f"http_{response.status_code}")

    async def refund(self, request: RefundRequest) -> RefundResult:
        response = await send(
            lambda: self._client.post(
                "/refunds",
                json={
                    "payment_reference": request.attempt_key,
                    "amount": request.money.amount_minor,
                },
                headers={"Idempotency-Key": request.idempotency_key},
            ),
            self._timeout,
        )
        if isinstance(response, ProviderOutcome):
            return RefundResult(succeeded=False, error=response.code)
        data = safe_json(response) or {}
        if response.status_code == 200 and data.get("status") == "refunded":
            return RefundResult(succeeded=True, refund_id=data.get("refund_id"))
        return RefundResult(
            succeeded=False, error=f"http_{response.status_code}:{data.get('error')}"
        )

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> WebhookEvent:
        signature = headers.get("x-alpha-signature", "")
        if not signature or not _compare(hmac_sha256_hex(self._secret, body), signature):
            raise InvalidWebhookError("bad signature")
        data = _load(body)
        payment = data.get("payment")
        if not isinstance(payment, dict) or "event_id" not in data:
            raise InvalidWebhookError("malformed body")
        outcome = self._parse(httpx.Response(200, json=payment))
        return WebhookEvent(
            event_id=str(data["event_id"]),
            event_type=str(data.get("type", "payment.updated")),
            kind=WebhookKind.PAYMENT_OUTCOME,
            attempt_key=payment.get("reference"),
            provider_payment_id=payment.get("id"),
            outcome=outcome,
            payload=data,
        )


class BetaProvider(_MockProviderBase):
    """Named-reason dialect. Signature: X-Signature: t=<unix>,v1=hex(HMAC(secret, "t.body"))."""

    signature_tolerance_seconds = 300

    async def charge(self, request: ChargeRequest) -> ProviderOutcome:
        body = {
            "amount": request.money.amount_minor,
            "currency": request.money.currency,
            "source": self._token(request),
            "reference": request.idempotency_key,
        }
        response = await send(
            lambda: self._client.post(
                "/v1/charges", json=body, headers={"Idempotency-Key": request.idempotency_key}
            ),
            self._timeout,
        )
        if isinstance(response, ProviderOutcome):
            return response
        return self._parse(response)

    async def get_status(self, request: StatusRequest) -> ProviderOutcome:
        response = await send(
            lambda: self._client.get(f"/v1/charges/by-reference/{request.charge.idempotency_key}"),
            self._timeout,
        )
        if isinstance(response, ProviderOutcome):
            return response
        if response.status_code == 404:
            # Same contract as alpha: references are stored on receipt.
            return ProviderOutcome.technical_error("not_found", "provider has no such charge")
        return self._parse(response)

    def _parse(self, response: httpx.Response) -> ProviderOutcome:
        data = safe_json(response) or {}
        charge = data.get("charge")
        if isinstance(charge, dict) and response.status_code in (200, 201, 402):
            return self._parse_charge(charge)
        error = data.get("error") if isinstance(data.get("error"), dict) else {}
        if response.status_code == 503 and error.get("processed") is False:
            return ProviderOutcome.technical_error(str(error.get("code")), "not processed")
        if response.status_code == 409:
            return ProviderOutcome.unknown("request_in_progress")
        if 400 <= response.status_code < 500:
            return ProviderOutcome.technical_error(f"http_{response.status_code}", str(error))
        # 500 without "processed": false: the charge may or may not exist.
        return ProviderOutcome.unknown(f"http_{response.status_code}")

    def _parse_charge(self, charge: dict[str, Any]) -> ProviderOutcome:
        state = charge.get("state")
        pid = charge.get("id")
        reason = charge.get("decline_reason")
        if state == "captured":
            return ProviderOutcome.approved(pid, "captured")
        if state == "pending":
            return ProviderOutcome(kind=K.UNKNOWN, provider_payment_id=pid, code="pending")
        if state in ("rejected", "failed"):
            kind = classify_decline(BETA_CODES, self.code, reason)
            return ProviderOutcome(kind=kind, provider_payment_id=pid, code=reason)
        return ProviderOutcome.unknown("unexpected_state", str(state))

    async def refund(self, request: RefundRequest) -> RefundResult:
        response = await send(
            lambda: self._client.post(
                "/v1/refunds",
                json={
                    "charge_reference": request.attempt_key,
                    "amount": request.money.amount_minor,
                },
                headers={"Idempotency-Key": request.idempotency_key},
            ),
            self._timeout,
        )
        if isinstance(response, ProviderOutcome):
            return RefundResult(succeeded=False, error=response.code)
        data = safe_json(response) or {}
        refund = data.get("refund") if isinstance(data.get("refund"), dict) else {}
        if response.status_code in (200, 201) and refund.get("state") == "succeeded":
            return RefundResult(succeeded=True, refund_id=refund.get("id"))
        return RefundResult(succeeded=False, error=f"http_{response.status_code}")

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> WebhookEvent:
        parts = dict(
            item.split("=", 1) for item in headers.get("x-signature", "").split(",") if "=" in item
        )
        timestamp, signature = parts.get("t", ""), parts.get("v1", "")
        if not timestamp.isdigit() or not signature:
            raise InvalidWebhookError("missing signature")
        if abs(time.time() - int(timestamp)) > self.signature_tolerance_seconds:
            raise InvalidWebhookError("timestamp outside tolerance")  # replay protection
        expected = hmac_sha256_hex(self._secret, timestamp.encode() + b"." + body)
        if not _compare(expected, signature):
            raise InvalidWebhookError("bad signature")
        data = _load(body)
        charge = (data.get("data") or {}).get("charge")
        if not isinstance(charge, dict) or "id" not in data:
            raise InvalidWebhookError("malformed body")
        event_type = str(data.get("type", ""))
        if not event_type.startswith("charge."):
            return WebhookEvent(
                event_id=str(data["id"]),
                event_type=event_type,
                kind=WebhookKind.IGNORED,
                payload=data,
            )
        return WebhookEvent(
            event_id=str(data["id"]),
            event_type=event_type,
            kind=WebhookKind.PAYMENT_OUTCOME,
            attempt_key=charge.get("reference"),
            provider_payment_id=charge.get("id"),
            outcome=self._parse_charge(charge),
            payload=data,
        )


def _compare(expected: str, given: str) -> bool:
    return hmac.compare_digest(expected, given)


def _load(body: bytes) -> dict[str, Any]:
    try:
        data = json.loads(body)
    except ValueError:
        raise InvalidWebhookError("body is not JSON") from None
    if not isinstance(data, dict):
        raise InvalidWebhookError("body is not an object")
    return data
