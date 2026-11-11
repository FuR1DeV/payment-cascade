"""SQLAlchemy Core tables. Kept in sync with migrations/versions by hand."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

metadata = sa.MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_name)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)

TS = sa.DateTime(timezone=True)

payments = sa.Table(
    "payments",
    metadata,
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
)

payment_attempts = sa.Table(
    "payment_attempts",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column(
        "payment_id",
        UUID(as_uuid=True),
        sa.ForeignKey("payments.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("attempt_no", sa.Integer, nullable=False),
    sa.Column("provider", sa.String(32), nullable=False),
    sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
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
    sa.UniqueConstraint("payment_id", "attempt_no", name="uq_payment_attempts_payment_attempt_no"),
)

webhook_events = sa.Table(
    "webhook_events",
    metadata,
    sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column("provider", sa.String(32), nullable=False),
    sa.Column("event_id", sa.String(255), nullable=False),
    sa.Column("event_type", sa.String(64), nullable=False),
    sa.Column("payload", JSONB, nullable=False),
    sa.Column("received_at", TS, nullable=False, server_default=sa.func.now()),
    sa.UniqueConstraint("provider", "event_id", name="uq_webhook_events_provider_event"),
)

payment_alerts = sa.Table(
    "payment_alerts",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column("payment_id", UUID(as_uuid=True), sa.ForeignKey("payments.id"), nullable=False),
    sa.Column("attempt_no", sa.Integer),
    sa.Column("kind", sa.String(32), nullable=False),
    sa.Column("message", sa.Text, nullable=False),
    sa.Column("details", JSONB, nullable=False),
    sa.Column("created_at", TS, nullable=False),
)

routing_rules = sa.Table(
    "routing_rules",
    metadata,
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
)
