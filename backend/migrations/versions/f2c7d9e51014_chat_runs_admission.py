"""Persistent request identities and shared concurrency leases."""
from alembic import op
import sqlalchemy as sa

revision = "f2c7d9e51014"
down_revision = "e1b6c8d41013"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("chat_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("app_id", sa.String(36), sa.ForeignKey("applications.id"), nullable=False),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("owner_token", sa.String(36), nullable=False),
        sa.Column("lease_expires_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.JSON(), nullable=True),
        sa.UniqueConstraint("user_id", "app_id", "request_id", name="uq_chat_run_request"))
    for field in ("user_id", "app_id", "state", "lease_expires_at"):
        op.create_index(f"ix_chat_runs_{field}", "chat_runs", [field])
    op.create_table("admission_leases",
        sa.Column("scope", sa.String(180), primary_key=True),
        sa.Column("slot", sa.Integer(), primary_key=True),
        sa.Column("owner_token", sa.String(36), nullable=True),
        sa.Column("expires_at", sa.Float(), nullable=False))
    for field in ("owner_token", "expires_at"):
        op.create_index(f"ix_admission_leases_{field}", "admission_leases", [field])


def downgrade():
    op.drop_table("admission_leases")
    op.drop_table("chat_runs")
