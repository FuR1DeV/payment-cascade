"""Shared HTTP plumbing for provider adapters: timeouts and error classification."""

import asyncio
import hashlib
import hmac
import logging
from collections.abc import Awaitable, Callable

import httpx

from app.modules.payments.domain.outcome import ProviderOutcome

logger = logging.getLogger("payments.providers")


async def send(
    request: Callable[[], Awaitable[httpx.Response]], total_timeout: float
) -> httpx.Response | ProviderOutcome:
    """Run an HTTP call. Network failures become outcomes, never exceptions.

    The key question for every failure: could the provider have received the request?
    - could not connect / no free connection: the request never left, safe to cascade;
    - anything after the request was (maybe) sent: unknown, we must ask for the status.
    asyncio.timeout bounds the whole call; httpx timeouts are per network operation.
    """
    try:
        async with asyncio.timeout(total_timeout):
            return await request()
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
        return ProviderOutcome.technical_error("connect_failed", type(exc).__name__)
    except (httpx.TimeoutException, TimeoutError):
        return ProviderOutcome.unknown("timeout")
    except httpx.TransportError as exc:
        return ProviderOutcome.unknown("transport_error", type(exc).__name__)


def unanswered_status_query(failure: httpx.Response | ProviderOutcome) -> ProviderOutcome:
    """A status query that got no real answer says nothing about the charge.

    For a charge request, "could not connect" or a 4xx means that request was not
    processed, so the next provider may be tried. For a status query the same failure
    only means the question went unanswered: the charge itself is still unknown, and
    reading it as "not charged" would let the cascade charge the card twice.
    """
    if isinstance(failure, httpx.Response):
        return ProviderOutcome.unknown(f"status_http_{failure.status_code}")
    return ProviderOutcome.unknown(f"status_{failure.code}", failure.message)


def hmac_sha256_hex(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def signature_matches(expected: str, given: str) -> bool:
    """Constant-time compare. Bytes, because compare_digest rejects non-ASCII str and a
    header from the outside world may contain anything."""
    return hmac.compare_digest(expected.encode(), given.encode("utf-8", "surrogateescape"))


def is_unix_timestamp(value: str) -> bool:
    # str.isdigit() alone accepts "²" and other Unicode digits that int() rejects.
    return value.isascii() and value.isdigit() and len(value) <= 12


def safe_json(response: httpx.Response) -> dict | None:
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None
