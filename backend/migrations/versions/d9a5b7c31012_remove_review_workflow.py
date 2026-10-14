"""Retire content-review workflow without rewriting knowledge or historical decisions."""
from alembic import op
import sqlalchemy as sa

revision = "d9a5b7c31012"
down_revision = "c8f4e6a21011"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    documents = sa.table("documents", sa.column("id", sa.String), sa.column("state", sa.String),
                         sa.column("parse_options", sa.JSON), sa.column("recognition", sa.JSON))
    connection.execute(documents.update().where(documents.c.state == "review").values(state="ready"))
    for doc in connection.execute(sa.select(documents)).mappings().all():
        options = {k: v for k, v in (doc["parse_options"] or {}).items() if k != "auto_review"}
        recognition = {k: v for k, v in (doc["recognition"] or {}).items() if k != "requires_review"}
        if "splitting" in recognition:
            recognition["splitting"] = {k: v for k, v in recognition["splitting"].items() if k != "requires_review"}
        connection.execute(documents.update().where(documents.c.id == doc["id"])
                           .values(parse_options=options, recognition=recognition))
    tasks = sa.table("tasks", sa.column("kind", sa.String), sa.column("state", sa.String), sa.column("error", sa.String))
    connection.execute(tasks.update().where(tasks.c.kind == "auto_review", tasks.c.state.in_(["pending", "running"]))
                       .values(state="cancelled", error="审核流程已移除，此历史任务不再执行"))


def downgrade():
    # Historical decisions and flags are retained; cancelled cloud jobs are never restarted.
    op.execute("UPDATE documents SET state = 'review' WHERE state = 'ready'")
