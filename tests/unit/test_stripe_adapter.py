"""Stripe adapter against canned Stripe API responses (no network)."""

import hashlib
import hmac
import json
import time
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.modules.payments.application.ports import ChargeRequest, InvalidWebhookError, StatusRequest
from app.modules.payments.domain.enums import OutcomeKind
from app.modules.payments.domain.money import Money
from app.modules.payments.infrastructure.providers.stripe import StripeProvider
from app.modules.payments.infrastructure.providers.vault import TestTokenVault

K = OutcomeKind


def stripe(
    responses: list[httpx.Response | Exception], seen: list[httpx.Request]
) -> StripeProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        reply = responses.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    return StripeProvider(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://stripe"),
        webhook_secret="whsec_test",
        timeout_seconds=5,
        vault=TestTokenVault(),
    )


def request(token: str = "tok_approve") -> ChargeRequest:
    pid = uuid4()
    return ChargeRequest(
        payment_id=pid,
        attempt_no=1,
        idempotency_key=f"{pid}:1",
        money=Money(amount_minor=1999, currency="USD"),
        payment_token=token,
        country="US",
    )


def card_error(decline_code: str) -> httpx.Response:
    return httpx.Response(
        402,
        json={
            "error": {
                "type": "card_error",
                "code": "card_declined",
                "decline_code": decline_code,
                "payment_intent": {"id": "pi_1"},
            }
        },
    )


@pytest.mark.parametrize(
    ("response", "kind"),
    [
        (httpx.Response(200, json={"id": "pi_1", "status": "succeeded"}), K.APPROVED),
        (card_error("generic_decline"), K.SOFT_DECLINE),
        (card_error("do_not_honor"), K.SOFT_DECLINE),
        (card_error("stolen_card"), K.HARD_DECLINE),
        (card_error("fraudulent"), K.HARD_DECLINE),
        (card_error("insufficient_funds"), K.HARD_DECLINE),
        (httpx.Response(200, json={"id": "pi_1", "status": "requires_action"}), K.HARD_DECLINE),
        (httpx.Response(200, json={"id": "pi_1", "status": "processing"}), K.UNKNOWN),
        (httpx.Response(429, json={"error": {"message": "rate limit"}}), K.TECHNICAL_ERROR),
        (httpx.Response(409, json={"error": {"code": "idempotency_key_in_use"}}), K.UNKNOWN),
        (httpx.Response(500, json={}), K.UNKNOWN),
    ],
)
async def test_stripe_responses(response: httpx.Response, kind: OutcomeKind) -> None:
    outcome = await stripe([response], []).charge(request())
    assert outcome.kind is kind


async def test_request_carries_test_payment_method_and_idempotency_key() -> None:
    seen: list[httpx.Request] = []
    req = request("tok_hard_decline")
    await stripe([httpx.Response(200, json={"id": "pi_1", "status": "succeeded"})], seen).charge(
        req
    )
    sent = seen[0]
    assert sent.headers["Idempotency-Key"] == req.idempotency_key
    form = dict(x.split("=", 1) for x in sent.content.decode().split("&"))
    assert form["payment_method"] == "pm_card_chargeDeclinedStolenCard"
    assert form["amount"] == "1999"
    assert form["currency"] == "usd"


NOTHING_FOUND = httpx.Response(200, json={"object": "search_result", "data": []})


async def test_status_by_known_intent_id_reads_the_intent() -> None:
    seen: list[httpx.Request] = []
    provider = stripe([httpx.Response(200, json={"id": "pi_9", "status": "processing"})], seen)
    outcome = await provider.get_status(StatusRequest(charge=request(), provider_payment_id="pi_9"))
    assert (outcome.kind, outcome.provider_payment_id) == (K.UNKNOWN, "pi_9")
    assert (seen[0].method, seen[0].url.path) == ("GET", "/v1/payment_intents/pi_9")


async def test_status_found_by_metadata_search_needs_no_replay() -> None:
    seen: list[httpx.Request] = []
    found = httpx.Response(200, json={"data": [{"id": "pi_5", "status": "succeeded"}]})
    req = request()
    outcome = await stripe([found], seen).get_status(
        StatusRequest(charge=req, attempt_age=timedelta(seconds=5))
    )
    assert (outcome.kind, outcome.provider_payment_id) == (K.APPROVED, "pi_5")
    assert len(seen) == 1
    assert seen[0].url.path == "/v1/payment_intents/search"
    assert req.idempotency_key in seen[0].url.params["query"]


async def test_young_attempt_not_found_is_replayed_with_same_key() -> None:
    seen: list[httpx.Request] = []
    req = request()
    replay = httpx.Response(200, json={"id": "pi_1", "status": "succeeded"})
    outcome = await stripe([NOTHING_FOUND, replay], seen).get_status(
        StatusRequest(charge=req, attempt_age=timedelta(seconds=5), replay_allowed=True)
    )
    assert outcome.kind is K.APPROVED
    assert seen[1].method == "POST"
    assert seen[1].headers["Idempotency-Key"] == req.idempotency_key


REFUSED = httpx.ConnectError("refused")
RATE_LIMITED = httpx.Response(429, json={"error": {"message": "rate limit"}})
BAD_KEY = httpx.Response(401, json={"error": {"message": "invalid api key"}})


@pytest.mark.parametrize("failure", [REFUSED, RATE_LIMITED, BAD_KEY])
async def test_failed_read_by_id_leaves_the_charge_unknown(
    failure: httpx.Response | Exception,
) -> None:
    # For a charge these failures mean "not processed"; for a status read they mean only
    # that the question went unanswered.
    outcome = await stripe([failure], []).get_status(
        StatusRequest(charge=request(), provider_payment_id="pi_9")
    )
    assert outcome.kind is K.UNKNOWN


async def test_failed_search_leaves_the_charge_unknown() -> None:
    seen: list[httpx.Request] = []
    outcome = await stripe([REFUSED], seen).get_status(
        StatusRequest(charge=request(), attempt_age=timedelta(seconds=5), replay_allowed=True)
    )
    assert outcome.kind is K.UNKNOWN
    assert len(seen) == 1  # no replay after a failed search


@pytest.mark.parametrize("failure", [REFUSED, RATE_LIMITED, BAD_KEY])
async def test_failed_replay_leaves_the_charge_unknown(failure: httpx.Response | Exception) -> None:
    outcome = await stripe([NOTHING_FOUND, failure], []).get_status(
        StatusRequest(charge=request(), attempt_age=timedelta(seconds=5), replay_allowed=True)
    )
    assert outcome.kind is K.UNKNOWN


async def test_replayed_card_decline_is_a_real_answer() -> None:
    outcome = await stripe([NOTHING_FOUND, card_error("do_not_honor")], []).get_status(
        StatusRequest(charge=request(), attempt_age=timedelta(seconds=5), replay_allowed=True)
    )
    assert (outcome.kind, outcome.code) == (K.SOFT_DECLINE, "do_not_honor")


async def test_old_attempt_is_never_replayed() -> None:
    """After 24 h Stripe forgets the key and a replay would be a new charge."""
    seen: list[httpx.Request] = []
    outcome = await stripe([NOTHING_FOUND], seen).get_status(
        StatusRequest(charge=request(), attempt_age=timedelta(hours=3), replay_allowed=True)
    )
    assert outcome.kind is K.UNKNOWN
    assert [r.method for r in seen] == ["GET"]


async def test_no_replay_once_the_client_has_an_answer() -> None:
    """Background checks run after the client got requires_review (or after another
    attempt settled the payment): a replay there could be a first, unexpected charge."""
    seen: list[httpx.Request] = []
    outcome = await stripe([NOTHING_FOUND], seen).get_status(
        StatusRequest(charge=request(), attempt_age=timedelta(seconds=5))
    )
    assert outcome.kind is K.UNKNOWN
    assert [r.method for r in seen] == ["GET"]


def test_only_mapped_tokens_are_routed_to_stripe() -> None:
    provider = stripe([], [])
    money = Money(amount_minor=100, currency="USD")
    assert provider.supports(money, "US", "tok_approve")
    assert not provider.supports(money, "US", "tok_timeout_stuck")


def test_webhook_signature() -> None:
    provider = stripe([], [])
    event = {
        "id": "evt_1",
        "type": "payment_intent.succeeded",
        "data": {
            "object": {"id": "pi_1", "status": "succeeded", "metadata": {"attempt_key": "p:1"}}
        },
    }
    body = json.dumps(event).encode()
    ts = str(int(time.time()))
    sig = hmac.new(b"whsec_test", ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    parsed = provider.parse_webhook({"stripe-signature": f"t={ts},v1={sig}"}, body)
    assert parsed.attempt_key == "p:1"
    assert parsed.outcome is not None
    assert parsed.outcome.kind is K.APPROVED
    with pytest.raises(InvalidWebhookError):
        provider.parse_webhook({"stripe-signature": f"t={ts},v1={'0' * 64}"}, body)


@pytest.mark.parametrize("header", ["t=²³,v1=abc", f"t={int(time.time())},v1=é"])
def test_garbage_signature_header_is_rejected(header: str) -> None:
    with pytest.raises(InvalidWebhookError):
        stripe([], []).parse_webhook({"stripe-signature": header}, b"{}")


def test_live_keys_are_rejected() -> None:
    with pytest.raises(ValidationError, match="test-mode"):
        Settings(stripe_secret_key="sk_live_123", _env_file=None)  # type: ignore[call-arg]
    assert Settings(stripe_secret_key="", _env_file=None).stripe_secret_key is None  # type: ignore[call-arg]
