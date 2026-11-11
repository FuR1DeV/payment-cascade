"""Run every scenario against the running stack and check the outcome.

    docker compose exec api python -m scripts.demo          # ~30 s
    docker compose exec api python -m scripts.demo --slow   # + review/reconcile, ~2 min

Talks to the API and to the mock PSP over the compose network, so it exercises the real
HTTP adapters, real timeouts, real webhooks and the Taskiq worker.
"""

import argparse
import os
import sys
import time
import uuid
from collections.abc import Callable
from typing import Any

import httpx

API = os.environ.get("DEMO_API_URL", "http://localhost:8000")
PSP = os.environ.get("DEMO_PSP_URL", "http://mock-psp:9000")
RESULTS: list[tuple[str, bool, str]] = []


def pay(client: httpx.Client, token: str, key: str | None = None, **extra: Any) -> httpx.Response:
    body = {
        "customer_id": "demo",
        "amount": 1999,
        "currency": "USD",
        "country": "DE",
        "payment_method": {"type": "card", "token": token},
    } | extra
    return client.post("/payments", json=body, headers={"Idempotency-Key": key or uuid.uuid4().hex})


def trail(payment: dict[str, Any]) -> str:
    steps = [f"{a['provider']}:{a['status']}" for a in payment["attempts"]]
    return f"{payment['status']} [{' -> '.join(steps)}]"


def check(name: str, condition: bool, detail: str) -> None:
    RESULTS.append((name, condition, detail))
    print(f"{'OK  ' if condition else 'FAIL'} {name:<38} {detail}", flush=True)


def wait_for(
    fetch: Callable[[], dict[str, Any]], ok: Callable[[dict[str, Any]], bool], timeout: float
):
    deadline = time.monotonic() + timeout
    data = fetch()
    while not ok(data) and time.monotonic() < deadline:
        time.sleep(1)
        data = fetch()
    return data


def simple(
    client: httpx.Client, name: str, token: str, expected: list[tuple[str, str]], status: str
) -> dict:
    payment = pay(client, token).json()
    got = [(a["provider"], a["status"]) for a in payment["attempts"]]
    check(name, payment["status"] == status and got == expected, trail(payment))
    return payment


def decided_by_status_check(payment: dict[str, Any], attempt_no: int) -> None:
    # The mock sends the webhook later than our status check in these scenarios, so the
    # answer must have come from the status check, not from the webhook.
    attempt = payment["attempts"][attempt_no - 1]
    check(
        "  ...decided by a status check",
        attempt["status_checks"] >= 1,
        f"attempt {attempt_no}: status_checks={attempt['status_checks']}",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slow", action="store_true", help="also run the requires_review scenario")
    args = parser.parse_args()
    client = httpx.Client(base_url=API, timeout=60)
    psp = httpx.Client(base_url=PSP, timeout=10)

    simple(client, "approve", "tok_approve", [("mock_alpha", "approved")], "succeeded")
    simple(
        client,
        "soft decline -> next provider",
        "tok_soft_decline",
        [("mock_alpha", "declined_soft"), ("mock_beta", "approved")],
        "succeeded",
    )
    simple(
        client,
        "hard decline stops",
        "tok_hard_decline",
        [("mock_alpha", "declined_hard")],
        "failed",
    )
    simple(
        client,
        "insufficient funds stops",
        "tok_insufficient_funds",
        [("mock_alpha", "declined_hard")],
        "failed",
    )
    simple(
        client,
        "all soft -> failed after 3",
        "tok_all_soft",
        [
            ("mock_alpha", "declined_soft"),
            ("mock_beta", "declined_soft"),
            ("mock_gamma", "declined_soft"),
        ],
        "failed",
    )
    p = simple(
        client,
        "timeout, charged -> no cascade",
        "tok_timeout_approved",
        [("mock_alpha", "approved")],
        "succeeded",
    )
    decided_by_status_check(p, 1)
    charges = psp.get("/_admin/charges", params={"reference_prefix": p["id"]}).json()
    check("  ...provider saw exactly one charge", len(charges) == 1, f"{len(charges)} charge(s)")
    p = simple(
        client,
        "timeout, not processed -> cascade",
        "tok_timeout_not_processed",
        [("mock_alpha", "error"), ("mock_beta", "approved")],
        "succeeded",
    )
    decided_by_status_check(p, 1)
    p = simple(
        client,
        "500 without guarantee -> status check",
        "tok_beta_server_error",
        [("mock_alpha", "declined_soft"), ("mock_beta", "approved")],
        "succeeded",
    )
    decided_by_status_check(p, 2)

    # Idempotency: the same key twice gives the same payment and no second charge.
    key = uuid.uuid4().hex
    first, second = pay(client, "tok_approve", key), pay(client, "tok_approve", key)
    check(
        "idempotent replay",
        (first.status_code, second.status_code) == (201, 200) and first.json() == second.json(),
        f"{first.status_code} then {second.status_code}, "
        f"replayed={second.headers.get('Idempotent-Replayed')}",
    )
    reused = pay(client, "tok_approve", key, amount=5000)
    check("same key, other body -> 422", reused.status_code == 422, str(reused.status_code))

    # Late webhook: alpha said "not processed", charged anyway, told us after beta charged.
    p = pay(client, "tok_late_webhook").json()
    check("late webhook: paid at beta", p["succeeded_provider"] == "mock_beta", trail(p))
    p = wait_for(
        lambda: client.get(f"/payments/{p['id']}").json(),
        lambda d: d["attempts"][0]["status"] == "refunded",
        timeout=30,
    )
    check("  ...extra alpha charge refunded", p["attempts"][0]["status"] == "refunded", trail(p))
    alerts = client.get("/alerts", params={"payment_id": p["id"]}).json()
    check(
        "  ...alert recorded",
        [a["kind"] for a in alerts] == ["extra_charge"],
        str([a["kind"] for a in alerts]),
    )
    refunds = [
        r
        for c in psp.get("/_admin/charges", params={"reference_prefix": p["id"]}).json()
        for r in c["refunds"]
    ]
    check("  ...provider shows one refund", len(refunds) == 1, f"{len(refunds)} refund(s)")

    if args.slow:
        p = pay(client, "tok_timeout_stuck").json()
        check("stuck timeout -> requires_review", p["status"] == "requires_review", trail(p))
        p = wait_for(
            lambda: client.get(f"/payments/{p['id']}").json(),
            lambda d: d["status"] != "requires_review",
            timeout=150,
        )
        check("  ...reconciled by background task", p["status"] == "succeeded", trail(p))

    # Circuit breaker last: it takes alpha out of rotation for a while.
    first_hop, sent = None, 0
    while sent < 25 and first_hop != "mock_beta":
        p = pay(client, "tok_alpha_down").json()
        first_hop, sent = p["attempts"][0]["provider"], sent + 1
    circuit = {s["provider"]: s for s in client.get("/stats/providers").json()}["mock_alpha"]
    check(
        "circuit opens for failing alpha",
        first_hop == "mock_beta" and circuit["circuit"]["state"] == "open",
        f"after {sent} payments first hop is {first_hop}; alpha circuit {circuit['circuit']}",
    )

    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
