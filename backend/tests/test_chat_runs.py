import asyncio
import concurrent.futures
import time
import uuid

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db import engine
from app.models import Application, ChatRun, Conversation, Knowledge, Message, User
from app.schemas import ChatInput
from app.services import chat_runs, learning
from app.services.admission import user_slot
from . import test_agent_evaluation

platform = test_agent_evaluation.platform  # Reuse the offline SQLite fixture without starting HTTP.


def identity(platform):
    with Session(engine()) as db:
        return platform["bundles"][0]["app"]["id"], db.scalar(select(User.id))


def claim(platform, data=None):
    app_id, user_id = identity(platform)
    with Session(engine()) as db:
        return chat_runs.claim_run(db, app_id, user_id, data or ChatInput(message="解释导数", request_id=str(uuid.uuid4())))


def valid_result(db, execution):
    app, releases = learning.scope(db, execution.app_id)
    return {"status": "answered", "answer": "资料回答", "citations": [],
            "versions": learning.versions(releases), "app_version": app.version}


def test_request_uuid_is_normalized_and_invalid_values_are_rejected():
    raw = str(uuid.uuid4())
    assert ChatInput(message="q", request_id=raw.upper()).request_id == raw
    with pytest.raises(ValidationError):
        ChatInput(message="q", request_id="not-a-uuid")


def test_concurrent_duplicate_claims_create_one_owner_and_payload_conflicts_fail(platform):
    app_id, user_id = identity(platform)
    data = ChatInput(message="解释导数", request_id=str(uuid.uuid4()))

    def request():
        with Session(engine()) as db:
            return chat_runs.claim_run(db, app_id, user_id, data)

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        claims = list(pool.map(lambda _: request(), range(6)))
    assert sum(item.created for item in claims) == 1
    assert len({item.run_id for item in claims}) == len({item.owner_token for item in claims}) == 1
    with Session(engine()) as db:
        run = db.get(ChatRun, claims[0].run_id)
        assert len(run.payload_sha256) == 64 and not hasattr(run, "question")
        with pytest.raises(HTTPException) as error:
            chat_runs.claim_run(db, app_id, user_id, data.model_copy(update={"message": "different"}))
        assert error.value.status_code == 409


def test_completion_and_message_share_transaction_and_replay_does_not_execute(platform):
    original = claim(platform)

    async def check():
        async with chat_runs.start_run(original.run_id, original.owner_token):
            with Session(engine()) as db:
                result = valid_result(db, original)
                conversation = Conversation(user_id=original.user_id, app_id=original.app_id, title="q")
                db.add(conversation)
                db.flush()
                result["conversation_id"] = conversation.id
                db.add(Message(conversation_id=conversation.id, question="q", result=result))
                chat_runs.complete_run(db, original.run_id, original.owner_token, result)
                db.rollback()
                assert db.get(ChatRun, original.run_id).state == "running"
                assert db.scalar(select(Message)) is None
                result = valid_result(db, original)
                conversation = Conversation(user_id=original.user_id, app_id=original.app_id, title="q")
                db.add(conversation)
                db.flush()
                result["conversation_id"] = conversation.id
                db.add(Message(conversation_id=conversation.id, question="q", result=result))
                chat_runs.complete_run(db, original.run_id, original.owner_token, result)
                db.commit()

    asyncio.run(check())
    with Session(engine()) as db:
        replay = chat_runs.claim_run(db, original.app_id, original.user_id,
                                    ChatInput(message="解释导数", request_id=original.request_id))
        assert replay.created is False and replay.state == "succeeded"
        assert replay.result["answer"] == "资料回答"
        assert db.scalar(select(Message)).result == replay.result
        assert "owner_token" not in chat_runs.get_run(db, original.app_id, original.user_id, original.request_id)


def test_scope_and_withdrawal_apply_to_replayed_results(platform):
    original = claim(platform)
    with Session(engine()) as db:
        chat_runs._begin(original.run_id, original.owner_token)
        result = valid_result(db, original)
        node = db.scalar(select(Knowledge))
        result["agent"] = {"evidence_ids": [node.id]}
        chat_runs.complete_run(db, original.run_id, original.owner_token, result)
        db.commit()
        other = User(username="other", password_hash="unused")
        db.add(other)
        db.commit()
        with pytest.raises(HTTPException) as error:
            chat_runs.get_run(db, original.app_id, other.id, original.request_id)
        assert error.value.status_code == 404
        node.withdrawn = True
        db.commit()
        view = chat_runs.get_run(db, original.app_id, original.user_id, original.request_id)
        assert view["result"]["status"] == "outdated" and "资料回答" not in view["result"]["answer"]
        app = db.get(Application, original.app_id)
        app.active = False
        db.commit()
        with pytest.raises(HTTPException) as error:
            chat_runs.get_run(db, original.app_id, original.user_id, original.request_id)
        assert error.value.status_code == 404


def test_expired_execution_never_restarts_or_accepts_stale_owner_completion(platform):
    original = claim(platform)
    with Session(engine()) as db:
        db.execute(update(ChatRun).where(ChatRun.id == original.run_id).values(lease_expires_at=time.time() - 1))
        db.commit()
        replay = chat_runs.claim_run(db, original.app_id, original.user_id,
                                    ChatInput(message="解释导数", request_id=original.request_id))
        assert not replay.created and replay.state == "failed" and replay.error["code"] == "worker_expired"
        with pytest.raises(HTTPException):
            chat_runs.complete_run(db, original.run_id, original.owner_token, valid_result(db, original))


def test_wrong_owner_and_duplicate_start_do_not_overwrite_active_run(platform):
    original = claim(platform)

    async def check():
        async with chat_runs.start_run(original.run_id, original.owner_token):
            for owner in (str(uuid.uuid4()), original.owner_token):
                with pytest.raises(HTTPException):
                    async with chat_runs.start_run(original.run_id, owner):
                        pytest.fail("must not run twice")
            with Session(engine()) as db:
                assert db.get(ChatRun, original.run_id).state == "running"
                with pytest.raises(HTTPException):
                    chat_runs.complete_run(db, original.run_id, "wrong", valid_result(db, original))
                db.rollback()
                chat_runs.complete_run(db, original.run_id, original.owner_token, valid_result(db, original))
                db.commit()

    asyncio.run(check())


@pytest.mark.parametrize("queued", [False, True])
def test_explicit_cancel_stops_owner_while_running_or_queued(platform, monkeypatch, queued):
    monkeypatch.setattr(chat_runs.settings(), "user_chat_concurrency", 1)
    original = claim(platform)

    async def check():
        entered = asyncio.Event()

        async def execute():
            async with chat_runs.start_run(original.run_id, original.owner_token,
                                           on_wait=lambda _: entered.set()):
                entered.set()
                await asyncio.Event().wait()

        async def cancel():
            task = asyncio.create_task(execute())
            await asyncio.wait_for(entered.wait(), 2)
            with Session(engine()) as db:
                result = chat_runs.cancel_run(db, original.app_id, original.user_id, original.request_id)
                assert result["state"] == "cancelled"
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2.5)

        if queued:
            async with user_slot(original.user_id):
                await cancel()
        else:
            await cancel()

    asyncio.run(check())
    with Session(engine()) as db:
        replay = chat_runs.get_run(db, original.app_id, original.user_id, original.request_id)
        assert replay["state"] == "cancelled" and db.scalar(select(Message)) is None


def test_cancel_does_not_overwrite_success(platform):
    original = claim(platform)
    chat_runs._begin(original.run_id, original.owner_token)
    with Session(engine()) as db:
        chat_runs.complete_run(db, original.run_id, original.owner_token, valid_result(db, original))
        db.commit()
        assert chat_runs.cancel_run(db, original.app_id, original.user_id, original.request_id)["state"] == "succeeded"
