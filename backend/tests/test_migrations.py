import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect, text


def test_empty_sqlite_upgrade_and_chat_run_downgrade(tmp_path):
    database = tmp_path / "migration.db"
    url = f"sqlite:///{database}"
    env = dict(os.environ, DATABASE_URL=url, STORAGE_DIR=str(tmp_path))
    root = Path(__file__).resolve().parents[2]

    def migrate(*arguments):
        result = subprocess.run([sys.executable, "-m", "alembic", *arguments], cwd=root, env=env,
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr

    migrate("upgrade", "head")
    db_engine = create_engine(url)
    inspection = inspect(db_engine)
    assert {"chat_runs", "admission_leases"} <= set(inspection.get_table_names())
    assert any(set(item["column_names"]) == {"user_id", "app_id", "request_id"}
               for item in inspection.get_unique_constraints("chat_runs"))
    assert {item["name"] for item in inspection.get_columns("documents")} >= {"parse_options", "recognition"}
    with db_engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "f2c7d9e51014"
    db_engine.dispose()
    migrate("downgrade", "e1b6c8d41013")
    db_engine = create_engine(url)
    assert "chat_runs" not in inspect(db_engine).get_table_names()
    assert "documents" in inspect(db_engine).get_table_names()
    db_engine.dispose()
