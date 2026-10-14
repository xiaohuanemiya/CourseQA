"""Keep generated relation provenance with the relation and published snapshots."""
from alembic import op
import sqlalchemy as sa

revision = "e1b6c8d41013"
down_revision = "d9a5b7c31012"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("relations", sa.Column("attributes", sa.JSON(), nullable=False, server_default="{}"))


def downgrade():
    op.drop_column("relations", "attributes")
