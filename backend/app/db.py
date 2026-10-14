from functools import lru_cache

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session

from .config import settings


class Base(DeclarativeBase):
    pass


@lru_cache
def engine():
    cfg = settings()
    cfg.storage_dir.mkdir(parents=True, exist_ok=True)
    kwargs = {"connect_args": {"check_same_thread": False}} if cfg.database_url.startswith("sqlite") else {}
    result = create_engine(cfg.database_url, pool_pre_ping=True, **kwargs)
    if cfg.database_url.startswith("sqlite"):
        @event.listens_for(result, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
    return result


def session():
    with Session(engine(), expire_on_commit=False) as db:
        yield db
