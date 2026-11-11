"""Mock PSP: three test providers in one process.

alpha        ISO 8583 style codes     POST /alpha/payments, GET /alpha/payments?reference=
beta, gamma  named decline reasons    POST /{p}/v1/charges, GET /{p}/v1/charges/by-reference/{r}

Both dialects: Idempotency-Key on writes (replayed responses), references stored on
receipt (so "not found" means "never received"), signed webhooks, refunds.
State is in memory: restart the container to reset it.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from .scenarios import Behavior, behavior_for

logger = logging.getLogger("mock_psp")

ALPHA_VIEW = {
    "approve": ("approved", "00", "Approved"),
    "soft": ("declined", "05", "Do not honor"),
    "hard": ("declined", "43", "Stolen card"),
    "insufficient": ("declined", "51", "Insufficient funds"),
    "tech_error": ("failed", "96", "System malfunction"),
    "server_error": ("failed", "96", "System malfunction"),
}
BETA_VIEW = {
    "approve": ("captured", None),
    "soft": ("rejected", "do_not_honor"),
    "hard": ("rejected", "stolen_card"),
    "insufficient": ("rejected", "insufficient_funds"),
    "tech_error": ("failed", "service_unavailable"),
    "server_error": ("failed", "service_unavailable"),
}


@dataclass
class Charge:
    provider: str
    id: str
    reference: str
    amount: int
    currency: str
    token: str
    behavior: Behavior
    received_at: float
    refunds: list[dict[str, Any]] = field(default_factory=list)

    @property
    def final_outcome(self) -> str:
        return self.behavior.final_outcome or self.behavior.outcome

    def current_outcome(self, scale: float) -> str:
        settled = time.monotonic() - self.received_at >= self.behavior.settle_after * scale
        return self.final_outcome if settled else "processing"


def alpha_view(c: Charge, outcome: str) -> dict[str, Any]:
    if outcome == "processing":
        status, code, message = "processing", None, "Processing"
    else:
        status, code, message = ALPHA_VIEW[outcome]
    return {
        "id": c.id,
        "reference": c.reference,
        "amount": c.amount,
        "currency": c.currency,
        "status": status,
        "response_code": code,
        "message": message,
    }


def beta_view(c: Charge, outcome: str) -> dict[str, Any]:
    if outcome == "processing":
        state, reason = "pending", None
    else:
        state, reason = BETA_VIEW[outcome]
    return {
        "id": c.id,
        "reference": c.reference,
        "amount": c.amount,
        "currency": c.currency,
        "state": state,
        "decline_reason": reason,
    }


def create_app(
    *,
    webhook_base_url: str | None,
    secrets: dict[str, str],
    time_scale: float = 1.0,
) -> FastAPI:
    app = FastAPI(title="Mock PSP")
    charges: dict[tuple[str, str], Charge] = {}
    responses: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {}
    in_progress: set[tuple[str, str]] = set()
    background: set[asyncio.Task[None]] = set()
    http = httpx.AsyncClient(timeout=5.0)

    def spawn(coro: Any) -> None:
        task = asyncio.create_task(coro)
        background.add(task)
        task.add_done_callback(background.discard)

    async def send_webhook(c: Charge) -> None:
        b = c.behavior
        await asyncio.sleep((b.settle_after + b.webhook_delay) * time_scale)
        if webhook_base_url is None:
            return
        outcome = c.final_outcome
        if c.provider == "alpha":
            body = json.dumps(
                {
                    "event_id": f"evt_{uuid.uuid4().hex}",
                    "type": "payment.updated",
                    "payment": alpha_view(c, outcome),
                }
            ).encode()
            signature = hmac.new(secrets["alpha"].encode(), body, hashlib.sha256).hexdigest()
            headers = {"X-Alpha-Signature": signature}
        else:
            view = beta_view(c, outcome)
            ts = str(int(time.time()))
            body = json.dumps(
                {
                    "id": f"evt_{uuid.uuid4().hex}",
                    "type": f"charge.{view['state']}",
                    "created": int(ts),
                    "data": {"charge": view},
                }
            ).encode()
            mac = hmac.new(secrets[c.provider].encode(), ts.encode() + b"." + body, hashlib.sha256)
            headers = {"X-Signature": f"t={ts},v1={mac.hexdigest()}"}
        headers["Content-Type"] = "application/json"
        url = f"{webhook_base_url.rstrip('/')}/mock_{c.provider}"
        for attempt in range(3):  # real providers retry until they get a 2xx
            try:
                response = await http.post(url, content=body, headers=headers)
                logger.info("webhook %s %s -> %s", c.provider, c.reference, response.status_code)
                if response.status_code < 300:
                    return
            except httpx.HTTPError as exc:
                logger.warning("webhook %s failed: %s", c.reference, exc)
            await asyncio.sleep((attempt + 1) * time_scale)

    async def charge(
        provider: str, key: str | None, amount: int, currency: str, token: str, reference: str
    ) -> tuple[int, dict[str, Any]]:
        if not key or not reference:
            return 400, {"error": "idempotency_key_and_reference_required"}
        idem = (provider, key)
        if idem in responses:
            return responses[idem]
        if idem in in_progress:
            return 409, {"error": {"code": "request_in_progress"}, "response_code": None}
        in_progress.add(idem)
        try:
            behavior = behavior_for(token, provider)
            c = Charge(
                provider=provider,
                id=f"{provider[:3]}_{uuid.uuid4().hex[:16]}",
                reference=reference,
                amount=amount,
                currency=currency,
                token=token,
                behavior=behavior,
                received_at=time.monotonic(),
            )
            charges[(provider, reference)] = c  # stored on receipt
            if behavior.webhook:
                spawn(send_webhook(c))
            await asyncio.sleep(behavior.response_delay * time_scale)
            result = sync_response(c)
            responses[idem] = result
            return result
        finally:
            in_progress.discard(idem)

    def sync_response(c: Charge) -> tuple[int, dict[str, Any]]:
        outcome = c.behavior.outcome
        if c.provider == "alpha":
            if outcome == "tech_error":
                return 503, {
                    "error": "system_malfunction",
                    "response_code": "96",
                    "processed": False,
                }
            if outcome == "server_error":
                return 500, {"error": "internal_error"}
            return 200, alpha_view(c, outcome)
        if outcome == "tech_error":
            return 503, {"error": {"code": "service_unavailable", "processed": False}}
        if outcome == "server_error":
            return 500, {"error": {"code": "internal_error"}}
        return (201 if outcome == "approve" else 402), {"charge": beta_view(c, outcome)}

    def refund(provider: str, key: str | None, reference: str) -> tuple[int, dict[str, Any]]:
        if not key:
            return 400, {"error": "idempotency_key_required"}
        idem = (provider, f"refund:{key}")
        if idem in responses:
            return responses[idem]
        c = charges.get((provider, reference))
        if c is None:
            return 404, {"error": "not_found"}
        if c.current_outcome(time_scale) != "approve":
            return 409, {"error": "not_refundable"}
        refund_id = f"ref_{uuid.uuid4().hex[:16]}"
        c.refunds.append({"id": refund_id, "amount": c.amount, "idempotency_key": key})
        if provider == "alpha":
            result = 200, {"refund_id": refund_id, "status": "refunded"}
        else:
            result = 201, {"refund": {"id": refund_id, "state": "succeeded"}}
        responses[idem] = result
        return result

    # --- alpha ---------------------------------------------------------------------------

    @app.post("/alpha/payments")
    async def alpha_create(
        request: Request, idempotency_key: str | None = Header(default=None)
    ) -> JSONResponse:
        b = await request.json()
        code, body = await charge(
            "alpha",
            idempotency_key,
            b.get("amount"),
            b.get("currency"),
            b.get("card_token", ""),
            b.get("reference", ""),
        )
        return JSONResponse(body, status_code=code)

    @app.get("/alpha/payments")
    async def alpha_status(reference: str) -> dict[str, Any]:
        c = charges.get(("alpha", reference))
        if c is None:
            raise HTTPException(404, detail="not_found")
        return alpha_view(c, c.current_outcome(time_scale))

    @app.post("/alpha/refunds")
    async def alpha_refund(
        request: Request, idempotency_key: str | None = Header(default=None)
    ) -> JSONResponse:
        b = await request.json()
        code, body = refund("alpha", idempotency_key, b.get("payment_reference", ""))
        return JSONResponse(body, status_code=code)

    # --- beta / gamma --------------------------------------------------------------------

    def check(provider: str) -> None:
        if provider not in ("beta", "gamma"):
            raise HTTPException(404, detail="unknown provider")

    @app.post("/{provider}/v1/charges")
    async def beta_create(
        provider: str, request: Request, idempotency_key: str | None = Header(default=None)
    ) -> JSONResponse:
        check(provider)
        b = await request.json()
        code, body = await charge(
            provider,
            idempotency_key,
            b.get("amount"),
            b.get("currency"),
            b.get("source", ""),
            b.get("reference", ""),
        )
        return JSONResponse(body, status_code=code)

    @app.get("/{provider}/v1/charges/by-reference/{reference}")
    async def beta_status(provider: str, reference: str) -> dict[str, Any]:
        check(provider)
        c = charges.get((provider, reference))
        if c is None:
            raise HTTPException(404, detail="not_found")
        return {"charge": beta_view(c, c.current_outcome(time_scale))}

    @app.post("/{provider}/v1/refunds")
    async def beta_refund(
        provider: str, request: Request, idempotency_key: str | None = Header(default=None)
    ) -> JSONResponse:
        check(provider)
        b = await request.json()
        code, body = refund(provider, idempotency_key, b.get("charge_reference", ""))
        return JSONResponse(body, status_code=code)

    # --- inspection ----------------------------------------------------------------------

    @app.get("/_admin/charges")
    async def admin_charges(reference_prefix: str | None = None) -> list[dict[str, Any]]:
        """What the providers think happened: charges and refunds. For the demo."""
        return [
            {
                "provider": c.provider,
                "id": c.id,
                "reference": c.reference,
                "amount": c.amount,
                "currency": c.currency,
                "token": c.token,
                "state": c.current_outcome(time_scale),
                "refunds": c.refunds,
            }
            for c in charges.values()
            if reference_prefix is None or c.reference.startswith(reference_prefix)
        ]

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


def app_from_env() -> FastAPI:
    logging.basicConfig(level=logging.INFO)
    return create_app(
        webhook_base_url=os.environ.get("WEBHOOK_TARGET_BASE_URL"),
        secrets={
            "alpha": os.environ.get("MOCK_ALPHA_WEBHOOK_SECRET", "dev-alpha-secret"),
            "beta": os.environ.get("MOCK_BETA_WEBHOOK_SECRET", "dev-beta-secret"),
            "gamma": os.environ.get("MOCK_GAMMA_WEBHOOK_SECRET", "dev-gamma-secret"),
        },
        time_scale=float(os.environ.get("MOCK_TIME_SCALE", "1.0")),
    )
