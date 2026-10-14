"""Persist model review evidence and decisions separately from editable knowledge.

Revision ID: c8f4e6a21011
Revises: b7e3c2d1f009
"""
from alembic import op
import sqlalchemy as sa

revision = "c8f4e6a21011"
down_revision = "b7e3c2d1f009"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("knowledge_reviews",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kb_id", sa.String(36), sa.ForeignKey("knowledge_bases.id"), nullable=False),
        sa.Column("knowledge_id", sa.String(36), sa.ForeignKey("knowledge.id"), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("profile_id", sa.String(36), sa.ForeignKey("model_profiles.id"), nullable=False),
        sa.Column("input_revision", sa.Integer(), nullable=False),
        sa.Column("output_revision", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("profile_signature", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(30), nullable=False),
        sa.Column("applied", sa.Boolean(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False))
    for key in ["kb_id", "knowledge_id", "task_id"]:
        op.create_index("ix_knowledge_reviews_"+key, "knowledge_reviews", [key])


def downgrade():
    op.drop_table("knowledge_reviews")
