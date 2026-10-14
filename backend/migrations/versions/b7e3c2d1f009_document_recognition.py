"""Document OCR options and recognition summary."""
from alembic import op
import sqlalchemy as sa

revision = "b7e3c2d1f009"
down_revision = "95d6c5c03558"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("documents",sa.Column("parse_options",sa.JSON(),nullable=False,server_default="{}"))
    op.add_column("documents",sa.Column("recognition",sa.JSON(),nullable=False,server_default="{}"))
    # SQLite cannot ALTER COLUMN DROP DEFAULT; Alembic rebuilds only on that dialect.
    with op.batch_alter_table("documents") as batch:
        batch.alter_column("parse_options", server_default=None)
        batch.alter_column("recognition", server_default=None)


def downgrade():
    op.drop_column("documents","recognition")
    op.drop_column("documents","parse_options")
