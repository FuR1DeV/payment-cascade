"""Adapters against the real mock PSP app (in process) and against broken networks."""

import hashlib
import hmac
import json
import time
from uuid import uuid4

import httpx
import pytest

from app.modules.payments.application.ports import (
    ChargeRequest,
    InvalidWebhookError,
    RefundRequest,
    StatusRequest,
)
from app.modules.payments.domain.enums import OutcomeKind
from app.modules.payments.domain.money import Money
from app.modules.payments.infrastructure.providers.mock_psp import AlphaProvider, BetaProvider
from app.modules.payments.infrastructure.providers.vault import TestTokenVault
from mock_psp.app import create_app

K = OutcomeKind
SECRETS = {"alpha": "s-alpha", "beta": "s-beta", "gamma": "s-gamma"}
MONEY = Money(amount_minor=1999, currency="USD")


@pytest.fixture
def psp():  # type: ignore[no-untyped-def]
    # time_scale 0.01: a 10 s mock delay becomes 0.1 s.
    return create_app(webhook_base_url=None, secrets=SECRETS, time_scale=0.01)


def client(app, path: str) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://psp{path}")


def alpha(app, timeout: float = 1.0) -> AlphaProvider:  # type: ignore[no-untyped-def]
    return AlphaProvider(
        code="mock_alpha",
        client=client(app, "/alpha"),
        webhook_secret=SECRETS["alpha"],
        timeout_seconds=timeout,
        vault=TestTokenVault(),
    )


def beta(app, timeout: float = 1.0) -> BetaProvider:  # type: ignore[no-untyped-def]
    return BetaProvider(
        code="mock_beta",
        client=client(app, "/beta"),
        webhook_secret=SECRETS["beta"],
        timeout_seconds=timeout,
        vault=TestTokenVault(),
    )


def charge(token: str) -> ChargeRequest:
    pid = uuid4()
    return ChargeRequest(
        payment_id=pid,
        attempt_no=1,
        idempotency_key=f"{pid}:1",
        money=MONEY,
        payment_token=token,
        country="DE",
    )


@pytest.mark.parametrize(
    ("token", "kind", "code"),
    [
        ("tok_approve", K.APPROVED, "00"),
        ("tok_soft_decline", K.SOFT_DECLINE, "05"),
        ("tok_hard_decline", K.HARD_DECLINE, "43"),
        ("tok_insufficient_funds", K.HARD_DECLINE, "51"),
        ("tok_alpha_down", K.TECHNICAL_ERROR, "96"),
    ],
)
async def test_alpha_codes_are_normalized(psp, token: str, kind: OutcomeKind, code: str) -> None:  # type: ignore[no-untyped-def]
    outcome = await alpha(psp).charge(charge(token))
    assert (outcome.kind, outcome.code) == (kind, code)


@pytest.mark.parametrize(
    ("token", "kind"),
    [
        ("tok_approve", K.APPROVED),
        ("tok_all_soft", K.SOFT_DECLINE),
        ("tok_beta_server_error", K.UNKNOWN),  # 500 without "processed": false
    ],
)
async def test_beta_codes_are_normalized(psp, token: str, kind: OutcomeKind) -> None:  # type: ignore[no-untyped-def]
    assert (await beta(psp).charge(charge(token))).kind is kind


def test_beta_hard_decline_reason() -> None:
    provider = BetaProvider(
        code="mock_beta",
        client=httpx.AsyncClient(),
        webhook_secret="x",
        timeout_seconds=1,
        vault=TestTokenVault(),
    )
    response = httpx.Response(
        402, json={"charge": {"id": "c1", "state": "rejected", "decline_reason": "stolen_card"}}
    )
    assert provider._parse(response).kind is K.HARD_DECLINE


async def test_timeout_is_unknown_and_status_check_finds_the_charge(psp) -> None:  # type: ignore[no-untyped-def]
    provider = alpha(psp, timeout=0.05)
    request = charge("tok_timeout_approved")
    assert (await provider.charge(request)).kind is K.UNKNOWN
    status = await provider.get_status(StatusRequest(charge=request))
    assert status.kind is K.APPROVED


async def test_timeout_then_status_says_not_processed(psp) -> None:  # type: ignore[no-untyped-def]
    provider = alpha(psp, timeout=0.05)
    request = charge("tok_timeout_not_processed")
    assert (await provider.charge(request)).kind is K.UNKNOWN
    status = await provider.get_status(StatusRequest(charge=request))
    assert (status.kind, status.code) == (K.TECHNICAL_ERROR, "96")


async def test_status_of_never_received_request_is_not_charged(psp) -> None:  # type: ignore[no-untyped-def]
    status = await alpha(psp).get_status(StatusRequest(charge=charge("tok_approve")))
    assert (status.kind, status.code) == (K.TECHNICAL_ERROR, "not_found")


async def test_charge_is_idempotent_by_key(psp) -> None:  # type: ignore[no-untyped-def]
    provider = alpha(psp)
    request = charge("tok_approve")
    first = await provider.charge(request)
    second = await provider.charge(request)
    assert first.provider_payment_id == second.provider_payment_id


async def test_refund_is_idempotent(psp) -> None:  # type: ignore[no-untyped-def]
    for provider in (alpha(psp), beta(psp)):
        request = charge("tok_approve")
        await provider.charge(request)
        refund = RefundRequest(
            idempotency_key=f"refund:{request.idempotency_key}",
            attempt_key=request.idempotency_key,
            provider_payment_id=None,
            money=MONEY,
        )
        first = await provider.refund(refund)
        second = await provider.refund(refund)
        assert first.succeeded
        assert second.succeeded
        assert first.refund_id == second.refund_id


async def test_cannot_refund_a_decline(psp) -> None:  # type: ignore[no-untyped-def]
    provider = alpha(psp)
    request = charge("tok_soft_decline")
    await provider.charge(request)
    result = await provider.refund(
        RefundRequest(
            idempotency_key="r1",
            attempt_key=request.idempotency_key,
            provider_payment_id=None,
            money=MONEY,
        )
    )
    assert result.succeeded is False


def broken(exc: Exception) -> AlphaProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return AlphaProvider(
        code="mock_alpha",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://x"),
        webhook_secret="x",
        timeout_seconds=1,
        vault=TestTokenVault(),
    )


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (httpx.ConnectError("refused"), K.TECHNICAL_ERROR),  # never reached the provider
        (httpx.ConnectTimeout("slow connect"), K.TECHNICAL_ERROR),
        (httpx.ReadTimeout("slow response"), K.UNKNOWN),  # request may have been processed
        (httpx.WriteTimeout("slow write"), K.UNKNOWN),
        (httpx.RemoteProtocolError("connection dropped"), K.UNKNOWN),
    ],
)
async def test_network_failures_are_classified_by_whether_request_left(
    exc: Exception, kind: OutcomeKind
) -> None:
    assert (await broken(exc).charge(charge("tok_approve"))).kind is kind


@pytest.mark.parametrize(
    "exc",
    [httpx.ConnectError("refused"), httpx.ConnectTimeout("slow"), httpx.ReadTimeout("slow")],
)
async def test_failed_status_query_leaves_the_charge_unknown(exc: Exception) -> None:
    # "Could not connect" proves that a charge request never left. For a status query it
    # only proves that the question was not asked: the earlier charge may still exist.
    outcome = await broken(exc).get_status(StatusRequest(charge=charge("tok_approve")))
    assert outcome.kind is K.UNKNOWN


def replying(cls: type[AlphaProvider] | type[BetaProvider], status: int, body: dict):  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    return cls(
        code=f"mock_{cls.__name__.removesuffix('Provider').lower()}",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://x"),
        webhook_secret="x",
        timeout_seconds=1,
        vault=TestTokenVault(),
    )


@pytest.mark.parametrize("cls", [AlphaProvider, BetaProvider])
@pytest.mark.parametrize(
    ("status", "body", "kind"),
    [
        # Only "not found" is an answer about the charge (the reference is stored on
        # receipt). Any other rejection of the status query leaves the charge unknown.
        (404, {"error": "not found"}, K.TECHNICAL_ERROR),
        (400, {"error": "bad request"}, K.UNKNOWN),
        (401, {"error": "bad key"}, K.UNKNOWN),
        (429, {"error": "slow down"}, K.UNKNOWN),
        (
            503,
            {"response_code": "96", "processed": False, "error": {"processed": False}},
            K.UNKNOWN,
        ),
    ],
)
async def test_rejected_status_query_leaves_the_charge_unknown(
    cls: type[AlphaProvider] | type[BetaProvider], status: int, body: dict, kind: OutcomeKind
) -> None:
    outcome = await replying(cls, status, body).get_status(
        StatusRequest(charge=charge("tok_approve"))
    )
    assert outcome.kind is kind


@pytest.mark.parametrize(
    ("status", "body", "kind"),
    [
        (500, {"error": "internal"}, K.UNKNOWN),
        (502, {}, K.UNKNOWN),
        (503, {"error": "busy"}, K.UNKNOWN),  # 503 without the "not processed" contract
        (503, {"response_code": "96", "processed": False}, K.TECHNICAL_ERROR),
        (400, {"error": "bad request"}, K.TECHNICAL_ERROR),
        (200, {"status": "approved", "response_code": "00"}, K.APPROVED),
        (200, {"status": "declined", "response_code": "77"}, K.HARD_DECLINE),  # unmapped code
    ],
)
def test_alpha_http_responses(status: int, body: dict, kind: OutcomeKind) -> None:
    provider = broken(RuntimeError())
    assert provider._parse(httpx.Response(status, json=body)).kind is kind


def test_alpha_webhook_signature() -> None:
    provider = broken(RuntimeError())
    body = json.dumps(
        {
            "event_id": "e1",
            "payment": {"id": "p", "reference": "r", "status": "approved", "response_code": "00"},
        }
    ).encode()
    good = hmac.new(b"x", body, hashlib.sha256).hexdigest()
    event = provider.parse_webhook({"x-alpha-signature": good}, body)
    assert event.outcome is not None
    assert event.outcome.kind is K.APPROVED
    assert event.attempt_key == "r"
    with pytest.raises(InvalidWebhookError):
        provider.parse_webhook({"x-alpha-signature": good}, body.replace(b"approved", b"declined"))
    with pytest.raises(InvalidWebhookError):
        provider.parse_webhook({}, body)


def test_beta_webhook_signature_and_replay_window() -> None:
    provider = BetaProvider(
        code="mock_beta",
        client=httpx.AsyncClient(),
        webhook_secret="x",
        timeout_seconds=1,
        vault=TestTokenVault(),
    )
    body = json.dumps(
        {
            "id": "e1",
            "type": "charge.captured",
            "data": {"charge": {"id": "c1", "reference": "r", "state": "captured"}},
        }
    ).encode()

    def header(ts: int) -> dict[str, str]:
        mac = hmac.new(b"x", str(ts).encode() + b"." + body, hashlib.sha256).hexdigest()
        return {"x-signature": f"t={ts},v1={mac}"}

    now = int(time.time())
    assert provider.parse_webhook(header(now), body).outcome.kind is K.APPROVED  # type: ignore[union-attr]
    with pytest.raises(InvalidWebhookError, match="tolerance"):
        provider.parse_webhook(header(now - 3600), body)


@pytest.mark.parametrize(
    "header",
    [
        {"x-alpha-signature": "é" * 64},  # non-ASCII: compare_digest(str) would raise
        {"x-signature": "t=²³,v1=abc"},  # Unicode digits pass isdigit() but not int()
        {"x-signature": "t=1700000000,v1=ü"},
    ],
)
def test_garbage_signature_headers_are_rejected_not_crashing(header: dict[str, str]) -> None:
    alpha_provider = broken(RuntimeError())
    beta_provider = BetaProvider(
        code="mock_beta",
        client=httpx.AsyncClient(),
        webhook_secret="x",
        timeout_seconds=1,
        vault=TestTokenVault(),
    )
    for provider in (alpha_provider, beta_provider):
        with pytest.raises(InvalidWebhookError):
            provider.parse_webhook(header, b"{}")
