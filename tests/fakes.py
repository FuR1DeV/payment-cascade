"""Scripted provider and helpers for tests. No network, fully deterministic."""

import asyncio
import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import Any

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

FAKE_SECRET = "fake-secret"


def approved(pid: str = "psp_1") -> ProviderOutcome:
    return ProviderOutcome.approved(pid, "ok")


def soft(code: str = "do_not_honor") -> ProviderOutcome:
    return ProviderOutcome(kind=OutcomeKind.SOFT_DECLINE, code=code)


def hard(code: str = "stolen_card") -> ProviderOutcome:
    return ProviderOutcome(kind=OutcomeKind.HARD_DECLINE, code=code)


def tech(code: str = "service_unavailable") -> ProviderOutcome:
    return ProviderOutcome.technical_error(code)


def unknown(code: str = "timeout") -> ProviderOutcome:
    return ProviderOutcome.unknown(code)


class FakeProvider:
    """charge: scripted outcomes, then approve. get_status: scripted, the last one repeats."""

    def __init__(
        self,
        code: str,
        *,
        charge: list[ProviderOutcome | Exception] | None = None,
        status: list[ProviderOutcome] | None = None,
        refund_ok: bool = True,
        delay: float = 0.0,
    ) -> None:
        self.code = code
        self.charge_script = list(charge or [])
        self.status_script = list(status or [])
        self.refund_ok = refund_ok
        self.delay = delay
        self.charge_calls: list[ChargeRequest] = []
        self.status_calls: list[StatusRequest] = []
        self.refund_calls: list[RefundRequest] = []

    def supports(self, money: Money, country: str, payment_token: str) -> bool:
        return True

    async def charge(self, request: ChargeRequest) -> ProviderOutcome:
        self.charge_calls.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        item = (
            self.charge_script.pop(0)
            if self.charge_script
            else approved(f"{self.code}_{request.idempotency_key}")
        )
        if isinstance(item, Exception):
            raise item
        return item

    async def get_status(self, request: StatusRequest) -> ProviderOutcome:
        self.status_calls.append(request)
        if not self.status_script:
            return unknown("processing")
        if len(self.status_script) > 1:
            return self.status_script.pop(0)
        return self.status_script[0]

    async def refund(self, request: RefundRequest) -> RefundResult:
        self.refund_calls.append(request)
        if self.refund_ok:
            return RefundResult(succeeded=True, refund_id=f"re_{len(self.refund_calls)}")
        return RefundResult(succeeded=False, error="refund_declined")

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> WebhookEvent:
        expected = hmac.new(FAKE_SECRET.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(headers.get("x-fake-signature", ""), expected):
            raise InvalidWebhookError("bad signature")
        data = json.loads(body)
        return WebhookEvent(
            event_id=data["event_id"],
            event_type="payment.updated",
            kind=WebhookKind.PAYMENT_OUTCOME,
            attempt_key=data.get("attempt_key"),
            provider_payment_id=data.get("provider_payment_id"),
            outcome=ProviderOutcome(
                kind=OutcomeKind(data["kind"]),
                provider_payment_id=data.get("provider_payment_id"),
                code=data.get("code"),
            ),
            payload=data,
        )


def signed_webhook(**data: Any) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(data).encode()
    signature = hmac.new(FAKE_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return body, {"X-Fake-Signature": signature, "Content-Type": "application/json"}


class RecordingJobs:
    def __init__(self) -> None:
        self.refund_kicks = 0

    async def kick_refunds(self) -> None:
        self.refund_kicks += 1
