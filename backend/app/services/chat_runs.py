"""Persistent, user-scoped idempotency and ownership for chat execution."""
import asyncio
import copy
import hashlib
import json
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass

import anyio
from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..db import engine
from ..models import ChatRun, User, now
from . import learning
from .admission import AdmissionError, drain, user_slot
from .async_db import run_db

ACTIVE = ("queued", "running")


@dataclass(frozen=True)
class Claim:
    run_id: str
    request_id: str
    owner_token: str
    created: bool
    state: str
    result: dict | None
    error: dict | None
    user_id: str
    app_id: str


def _expired(db, run):
    if run.state in ACTIVE and run.lease_expires_at <= time.time():
        changed = db.execute(update(ChatRun).where(ChatRun.id == run.id, ChatRun.state.in_(ACTIVE),
            ChatRun.lease_expires_at <= time.time()).values(state="failed", updated_at=now(),
            error={"code": "worker_expired", "message": "上次执行已中断且租约过期，请使用新的请求编号重试。"}))
        if changed.rowcount:
            db.commit()
        db.refresh(run)


def _view(run, app, releases):
    result = copy.deepcopy(run.result)
    if result is not None:
        valid = {node["id"] for _, snapshot in releases for node in snapshot["nodes"]}
        citations = {citation.get("knowledge_id") for citation in result.get("citations", [])}
        used = set(result.get("agent", {}).get("evidence_ids", []))
        if (result.get("versions") != learning.versions(releases) or
                result.get("app_version") != app.version or not (citations | used) <= valid):
            result = {"status": "outdated", "answer": "此回答依据的应用或资料已更新或撤回，请重新提问。",
                      "citations": [], "conversation_id": result.get("conversation_id")}
    return {"run_id": run.id, "request_id": run.request_id, "state": run.state,
            "result": result, "error": copy.deepcopy(run.error)}


def _as_claim(run, created, app, releases):
    view = _view(run, app, releases)
    return Claim(run.id, run.request_id, run.owner_token, created, run.state, view["result"],
                 view["error"], run.user_id, run.app_id)


def claim_run(db, app_id, user_id, data):
    """Create or reuse an identity; this function commits only a short request transaction.

    Call on a dedicated request session before beginning generation. A duplicate is
    never reactivated, including after failure, cancellation or owner lease expiry.
    """
    app, releases = learning.scope(db, app_id, "chat")
    if db.get(User, user_id) is None:
        raise HTTPException(404, "用户不存在")
    learning.chat_conversation(db, app_id, user_id, data.conversation_id)
    request_id = data.request_id or str(uuid.uuid4())
    digest = hashlib.sha256(json.dumps(data.model_dump(exclude={"request_id"}),
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    where = (ChatRun.user_id == user_id, ChatRun.app_id == app_id, ChatRun.request_id == request_id)
    existing = db.scalar(select(ChatRun).where(*where))
    if existing is None:
        cfg = settings()
        run = ChatRun(user_id=user_id, app_id=app_id, request_id=request_id, payload_sha256=digest,
                      owner_token=str(uuid.uuid4()), state="queued",
                      lease_expires_at=time.time() + cfg.user_chat_queue_timeout + cfg.admission_lease_seconds)
        db.add(run)
        try:
            db.commit()
            return _as_claim(run, True, app, releases)
        except IntegrityError:
            db.rollback()
            existing = db.scalar(select(ChatRun).where(*where))
            if existing is None:
                raise
    if existing.payload_sha256 != digest:
        raise HTTPException(409, "相同 request_id 对应的消息或参数不同，请使用新的请求编号。")
    _expired(db, existing)
    return _as_claim(existing, False, app, releases)


def get_run(db, app_id, user_id, request_id):
    """Return only the requesting user's execution, with current withdrawal checks."""
    app, releases = learning.scope(db, app_id, "chat")
    run = db.scalar(select(ChatRun).where(ChatRun.user_id == user_id, ChatRun.app_id == app_id,
                                        ChatRun.request_id == str(request_id)))
    if run is None:
        raise HTTPException(404, "请求不存在")
    _expired(db, run)
    return _view(run, app, releases)


def cancel_run(db, app_id, user_id, request_id):
    """Make cancellation durable; the active owner's heartbeat stops its execution."""
    get_run(db, app_id, user_id, request_id)
    db.execute(update(ChatRun).where(ChatRun.user_id == user_id, ChatRun.app_id == app_id,
        ChatRun.request_id == str(request_id), ChatRun.state.in_(ACTIVE)).values(
        state="cancelled", updated_at=now(),
        error={"code": "cancelled", "message": "请求已取消，可使用新的请求编号重试。"}))
    db.commit()
    return get_run(db, app_id, user_id, request_id)


def complete_run(db, run_id, owner_token, result):
    """Update atomically with Message creation; the caller owns commit/rollback."""
    changed = db.execute(update(ChatRun).where(ChatRun.id == run_id, ChatRun.owner_token == owner_token,
        ChatRun.state == "running", ChatRun.lease_expires_at > time.time()).values(
        state="succeeded", result=copy.deepcopy(result), error=None, updated_at=now()))
    if changed.rowcount != 1:
        raise HTTPException(409, "请求执行权已失效，本次回答不能保存，请重新提问。")


def _owned(run_id, owner_token):
    with Session(engine()) as db:
        run = db.scalar(select(ChatRun).where(ChatRun.id == run_id, ChatRun.owner_token == owner_token))
        if run is None:
            raise HTTPException(404, "请求不存在")
        _expired(db, run)
        if run.state != "queued":
            raise HTTPException(409, "此请求已经开始或结束，不能重复执行。")
        return Claim(run.id, run.request_id, run.owner_token, True, run.state, None, None, run.user_id, run.app_id)


def _begin(run_id, owner_token):
    with Session(engine()) as db:
        changed = db.execute(update(ChatRun).where(ChatRun.id == run_id, ChatRun.owner_token == owner_token,
            ChatRun.state == "queued", ChatRun.lease_expires_at > time.time()).values(
            state="running", updated_at=now(), lease_expires_at=time.time() + settings().admission_lease_seconds))
        db.commit()
        if changed.rowcount != 1:
            raise HTTPException(409, "请求已失效或已被执行，请查询已有状态。")


def _renew_run(run_id, owner_token):
    with Session(engine()) as db:
        changed = db.execute(update(ChatRun).where(ChatRun.id == run_id, ChatRun.owner_token == owner_token,
            ChatRun.state.in_(ACTIVE), ChatRun.lease_expires_at > time.time()).values(
            lease_expires_at=time.time() + settings().admission_lease_seconds, updated_at=now()))
        db.commit()
        if changed.rowcount:
            return "active"
        # The answer can commit just before the heartbeat. Success is not a lost lease.
        state = db.scalar(select(ChatRun.state).where(ChatRun.id == run_id,
            ChatRun.owner_token == owner_token))
        return state if state in {"succeeded", "cancelled"} else "lost"


def finish_run(run_id, owner_token, state, error=None, *, expected_states=ACTIVE):
    if state not in {"failed", "cancelled"}:
        raise ValueError("finish_run accepts only failed/cancelled")
    with Session(engine()) as db:
        db.execute(update(ChatRun).where(ChatRun.id == run_id, ChatRun.owner_token == owner_token,
            ChatRun.state.in_(expected_states)).values(state=state, error=error, updated_at=now()))
        db.commit()


def _state(run_id, owner_token):
    with Session(engine()) as db:
        return db.scalar(select(ChatRun.state).where(ChatRun.id == run_id, ChatRun.owner_token == owner_token))


@asynccontextmanager
async def start_run(run_id, owner_token, *, on_wait=None):
    """Own only a user slot; nested provider calls independently acquire model slots."""
    heartbeat, lost, activated = None, [], False
    try:
        claim = await run_db(_owned, run_id, owner_token)
        holder = asyncio.current_task()

        async def renew():
            try:
                while True:
                    await asyncio.sleep(min(1, settings().admission_lease_seconds / 3))
                    state = await run_db(_renew_run, run_id, owner_token)
                    if state == "cancelled":
                        holder.cancel()
                        return
                    if state == "lost":
                        raise RuntimeError("run lease lost")
            except asyncio.CancelledError:
                raise
            except Exception:
                lost.append(True)
                holder.cancel()

        heartbeat = asyncio.create_task(renew())
        async with user_slot(claim.user_id, on_wait=on_wait):
            def begin():
                nonlocal activated
                _begin(run_id, owner_token)
                activated = True

            await run_db(begin)
            try:
                yield claim
            except asyncio.CancelledError:
                if lost:
                    raise AdmissionError("run_lease_lost", "请求执行权已失效，请重新发起请求。", 503) from None
                raise
            if await run_db(_state, run_id, owner_token) != "succeeded":
                raise AdmissionError("run_incomplete", "本次请求未保存完成，请重新发起请求。", 503)
    except asyncio.CancelledError:
        await drain(heartbeat)
        heartbeat = None
        with anyio.CancelScope(shield=True):
            await run_db(finish_run, run_id, owner_token, "cancelled",
                         {"code": "cancelled", "message": "请求已取消，可使用新的请求编号重试。"},
                         expected_states=ACTIVE if activated else ("queued",))
        raise
    except Exception as exc:
        await drain(heartbeat)
        heartbeat = None
        with anyio.CancelScope(shield=True):
            await run_db(finish_run, run_id, owner_token, "failed",
                         {"code": getattr(exc, "code", "execution_failed"),
                          "message": "本次执行未完成，请使用新的请求编号重试。"},
                         expected_states=ACTIVE if activated else ("queued",))
        raise
    finally:
        await drain(heartbeat)
