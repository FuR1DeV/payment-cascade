"""Initial schema: payments, attempts, webhook journal, alerts, routing rules.

Revision ID: 0001
Revises:
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)
PAYMENT_STATUSES = "('created','processing','succeeded','failed','requires_review')"
ATTEMPT_STATUSES = (
    "('pending','approved','declined_soft','declined_hard','error','unknown',"
    "'refund_pending','refunded')"
)


def upgrade() -> None:
    op.create_table(
        "payments",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("customer_id", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("amount_minor", sa.BigInteger, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("country", sa.String(2), nullable=False),
        sa.Column("method", sa.String(16), nullable=False),
        sa.Column("payment_token", sa.String(64), nullable=False),
        sa.Column("description", sa.String(255)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("route", ARRAY(sa.String(32)), nullable=False),
        sa.Column("succeeded_attempt_no", sa.Integer),
        sa.Column("failure_reason", sa.String(255)),
        sa.Column("review_reason", sa.String(255)),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("deadline_at", TS, nullable=False),
        sa.UniqueConstraint("customer_id", "idempotency_key", name="uq_payments_idempotency"),
        sa.CheckConstraint("amount_minor > 0", name="ck_payments_amount_positive"),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_payments_currency_iso"),
        sa.CheckConstraint(f"status IN {PAYMENT_STATUSES}", name="ck_payments_status"),
    )
    # Stale payment recovery scans by status and deadline.
    op.create_index("ix_payments_status_deadline", "payments", ["status", "deadline_at"])

    op.create_table(
        "payment_attempts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "payment_id",
            UUID(as_uuid=True),
            sa.ForeignKey("payments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt_no", sa.Integer, nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("provider_payment_id", sa.String(128)),
        sa.Column("provider_code", sa.String(64)),
        sa.Column("provider_message", sa.String(500)),
        sa.Column("late_approval", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("status_checks", sa.Integer, nullable=False, server_default="0"),
        sa.Column("next_status_check_at", TS),
        sa.Column("refund_id", sa.String(128)),
        sa.Column("refund_attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("next_refund_at", TS),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("finished_at", TS),
        sa.UniqueConstraint(
            "payment_id", "attempt_no", name="uq_payment_attempts_payment_attempt_no"
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_payment_attempts_idempotency_key"),
        sa.CheckConstraint("attempt_no >= 1", name="ck_payment_attempts_attempt_no"),
        sa.CheckConstraint(f"status IN {ATTEMPT_STATUSES}", name="ck_payment_attempts_status"),
    )
    # The database itself guarantees: at most one attempt per payment whose outcome is
    # not known yet. A second in-flight attempt (a second charge) cannot be inserted.
    op.create_index(
        "uq_payment_attempts_one_in_flight",
        "payment_attempts",
        ["payment_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending','unknown')"),
    )
    op.create_index(
        "uq_payment_attempts_provider_payment_id",
        "payment_attempts",
        ["provider", "provider_payment_id"],
        unique=True,
        postgresql_where=sa.text("provider_payment_id IS NOT NULL"),
    )
    op.create_index(
        "ix_payment_attempts_due_status_check",
        "payment_attempts",
        ["next_status_check_at"],
        postgresql_where=sa.text("status = 'unknown'"),
    )
    op.create_index(
        "ix_payment_attempts_due_refund",
        "payment_attempts",
        ["next_refund_at"],
        postgresql_where=sa.text("status = 'refund_pending'"),
    )
    op.create_index(
        "ix_payment_attempts_provider_created", "payment_attempts", ["provider", "created_at"]
    )

    op.create_table(
        "webhook_events",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("event_id", sa.String(255), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("received_at", TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("provider", "event_id", name="uq_webhook_events_provider_event"),
    )
    # Raw provider events are evidence: forbid UPDATE and DELETE at the database level.
    op.execute(
        """
        CREATE FUNCTION webhook_events_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'webhook_events is append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER webhook_events_append_only
        BEFORE UPDATE OR DELETE ON webhook_events
        FOR EACH ROW EXECUTE FUNCTION webhook_events_append_only()
        """
    )

    op.create_table(
        "payment_alerts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("payment_id", UUID(as_uuid=True), sa.ForeignKey("payments.id"), nullable=False),
        sa.Column("attempt_no", sa.Integer),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("details", JSONB, nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_index("ix_payment_alerts_payment_id", "payment_alerts", ["payment_id"])
    op.create_index("ix_payment_alerts_created_at", "payment_alerts", ["created_at"])

    rules = op.create_table(
        "routing_rules",
        sa.Column("id", sa.Integer, sa.Identity(), primary_key=True),
        sa.Column("priority", sa.Integer, nullable=False),
        sa.Column("currency", sa.String(3)),
        sa.Column("country", sa.String(2)),
        sa.Column("method", sa.String(16)),
        sa.Column("min_amount_minor", sa.BigInteger),
        sa.Column("max_amount_minor", sa.BigInteger),
        sa.Column("providers", ARRAY(sa.String(32)), nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("description", sa.String(255)),
        sa.CheckConstraint("cardinality(providers) > 0", name="ck_routing_rules_providers"),
    )
    empty = {"currency": None, "country": None, "method": None}
    empty |= {"min_amount_minor": None, "max_amount_minor": None}
    op.bulk_insert(
        rules,
        [
            empty
            | {
                "priority": 10,
                "country": "US",
                "method": "card",
                "providers": ["stripe", "mock_alpha", "mock_beta"],
                "description": "US cards: Stripe first (skipped when no Stripe key is set)",
            },
            empty
            | {
                "priority": 20,
                "currency": "EUR",
                "providers": ["mock_beta", "mock_alpha", "mock_gamma"],
                "description": "EUR: beta first (example of a currency rule)",
            },
            empty
            | {
                "priority": 100,
                "providers": ["mock_alpha", "mock_beta", "mock_gamma"],
                "description": "Default route",
            },
        ],
    )


def downgrade() -> None:
    op.drop_table("routing_rules")
    op.drop_table("payment_alerts")
    op.execute("DROP TRIGGER IF EXISTS webhook_events_append_only ON webhook_events")
    op.execute("DROP FUNCTION IF EXISTS webhook_events_append_only()")
    op.drop_table("webhook_events")
    op.drop_table("payment_attempts")
    op.drop_table("payments")
