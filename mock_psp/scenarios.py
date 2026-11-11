"""Deterministic behaviour of the mock providers, selected by the card token.

Each token describes how alpha, beta and gamma react. Providers not listed approve.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Behavior:
    # What the synchronous response says:
    # approve | soft | hard | insufficient | tech_error (not processed) | server_error (unknown)
    outcome: str = "approve"
    # Seconds before the HTTP response is sent. Above the client timeout means a timeout.
    response_delay: float = 0.0
    # Seconds after receipt when the final state becomes visible to status queries.
    settle_after: float = 0.0
    # Final state if it differs from the synchronous answer (provider "changes its mind").
    final_outcome: str | None = None
    webhook: bool = True
    # Seconds after settlement when the webhook is sent.
    webhook_delay: float = 0.5


SCENARIOS: dict[str, dict[str, Behavior]] = {
    "tok_approve": {},
    # A soft decline at the first provider, approval at the second.
    "tok_soft_decline": {"alpha": Behavior(outcome="soft")},
    # Stolen card: hard decline, the cascade stops.
    "tok_hard_decline": {"alpha": Behavior(outcome="hard")},
    "tok_insufficient_funds": {"alpha": Behavior(outcome="insufficient")},
    "tok_all_soft": {p: Behavior(outcome="soft") for p in ("alpha", "beta", "gamma")},
    # Timeout, but alpha did charge: the status check finds it, no cascade.
    "tok_timeout_approved": {"alpha": Behavior(response_delay=10, settle_after=1)},
    # Timeout, alpha did not process it: the status check says so, cascade to beta.
    "tok_timeout_not_processed": {
        "alpha": Behavior(outcome="tech_error", response_delay=10, settle_after=1)
    },
    # Timeout and alpha stays "processing" for a minute, no webhook: requires_review,
    # then the background reconciliation finds the approval.
    "tok_timeout_stuck": {"alpha": Behavior(response_delay=10, settle_after=60, webhook=False)},
    # Alpha answers "not processed" but charges anyway and says so in a late webhook,
    # after the payment already succeeded at beta: extra charge, refund.
    "tok_late_webhook": {
        "alpha": Behavior(outcome="tech_error", final_outcome="approve", webhook_delay=3)
    },
    # Alpha is down (technical errors): cascade to beta; repeated, it opens the circuit.
    "tok_alpha_down": {"alpha": Behavior(outcome="tech_error", webhook=False)},
    # Beta returns 500 without saying whether it processed the request; it did approve.
    "tok_beta_server_error": {
        "alpha": Behavior(outcome="soft"),
        "beta": Behavior(outcome="server_error", final_outcome="approve"),
    },
}


def behavior_for(token: str, provider: str) -> Behavior:
    return SCENARIOS.get(token, {}).get(provider, Behavior())
