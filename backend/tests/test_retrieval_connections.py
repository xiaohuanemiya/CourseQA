import pytest
from fastapi import HTTPException
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from app.db import engine
from app.models import Application, ModelProfile, User
from app.schemas import ChatInput
from app.services import learning, retrieval


@pytest.mark.parametrize("expire_on_commit", [True, False])
def test_chat_embedding_releases_connection_and_keeps_original_configuration(
        platform, monkeypatch, expire_on_commit):
    app_id = platform["bundles"][0]["app"]["id"]
    generation_id = platform["generation"]["id"]
    original_embed, observed = retrieval.embed, []
    with Session(engine(), expire_on_commit=expire_on_commit) as db:
        user_id = db.scalar(select(User.id).where(User.username == "admin"))
        old_version = db.get(Application, app_id).version
        old_model = db.get(ModelProfile, generation_id).model

        def embed(texts, profile):
            # Access every field needed by the real provider/admission wrapper;
            # a default-expiring ORM object would silently reacquire a connection.
            observed.append((profile.id, profile.provider, profile.model, profile.base_url,
                             profile.secret, profile.dimension, profile.temperature))
            assert inspect(profile, raiseerr=False) is None
            assert not db.in_transaction()
            assert engine().pool.checkedout() == 0
            with Session(engine()) as other:
                other.get(Application, app_id).version += 1
                other.get(ModelProfile, generation_id).model = "changed-during-embedding"
                other.commit()
            return original_embed(texts, profile)

        monkeypatch.setattr(retrieval, "embed", embed)
        app, versions, _, evidence, profile, result = learning.prepare_chat(
            db, app_id, user_id, ChatInput(message="导数知识"), release_read_connection=True)
        assert observed and evidence
        db.commit()
        assert app.version == result["app_version"] == old_version
        assert profile.model == old_model
        assert profile.id == generation_id and app.id == app_id
        assert result["versions"] == versions
        assert not db.in_transaction()
        with pytest.raises(HTTPException, match="版本已变化"):
            learning.assert_current(db, app, versions)


@pytest.mark.parametrize("flushed", [False, True])
def test_default_retrieval_never_commits_callers_pending_or_flushed_writes(platform, flushed):
    app_id = platform["bundles"][0]["app"]["id"]
    with Session(engine()) as db:
        app, releases = learning.scope(db, app_id)
        original_version = app.version
        app.version += 1
        if flushed:
            db.flush()
        assert retrieval.retrieve(db, app, releases, "导数知识")
        assert db.in_transaction()
        db.rollback()
    with Session(engine()) as db:
        assert db.get(Application, app_id).version == original_version


def test_opted_in_read_session_rejects_pending_writes_before_autoflush(platform):
    app_id = platform["bundles"][0]["app"]["id"]
    with Session(engine()) as db:
        app, releases = learning.scope(db, app_id)
        original_version = app.version
        app.version += 1
        with pytest.raises(RuntimeError, match="dedicated read-only session"):
            retrieval.retrieve(db, app, releases, "导数知识", release_read_connection=True)
        assert app in db.dirty
        db.rollback()
    with Session(engine()) as db:
        assert db.get(Application, app_id).version == original_version
