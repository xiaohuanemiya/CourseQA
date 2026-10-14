"""Database-backed, bounded admission shared by API workers and background processes."""
import asyncio
import inspect
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

import anyio
from fastapi import HTTPException
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..db import engine
from ..models import AdmissionLease
from .async_db import run_db, worker_cancellation

_progress = ContextVar("admission_progress", default=None)


def set_progress(callback):
    return _progress.set(callback)


def reset_progress(token):
    _progress.reset(token)


class AdmissionError(HTTPException):
    def __init__(self, code, message, status_code=429):
        super().__init__(status_code=status_code, detail=message)
        self.code = code


def check_worker_cancelled():
    signal = worker_cancellation.get()
    if signal is not None and signal.is_set():
        raise AdmissionError("cancelled", "请求已取消，可使用新的请求编号重试。", 409)


@dataclass(frozen=True)
class Lease:
    scope: str
    slot: int
    owner_token: str


def _claim(scope, capacity, owner, ttl):
    """Compare-and-swap one slot in a short transaction; uniqueness arbitrates first use."""
    if capacity <= 0:
        return None
    now = time.time()
    with Session(engine()) as db:
        rows = {row.slot: row for row in db.execute(select(
            AdmissionLease.slot, AdmissionLease.owner_token, AdmissionLease.expires_at
        ).where(AdmissionLease.scope == scope))}
        for slot in range(capacity):
            row = rows.get(slot)
            if row and row.owner_token is not None and row.expires_at > now:
                continue
            if row:
                changed = db.execute(update(AdmissionLease).where(
                    AdmissionLease.scope == scope, AdmissionLease.slot == slot,
                    or_(AdmissionLease.owner_token.is_(None), AdmissionLease.expires_at <= now)
                ).values(owner_token=owner, expires_at=now + ttl))
                if changed.rowcount:
                    db.commit()
                    return Lease(scope, slot, owner)
                db.rollback()
            else:
                db.add(AdmissionLease(scope=scope, slot=slot, owner_token=owner, expires_at=now + ttl))
                try:
                    db.commit()
                    return Lease(scope, slot, owner)
                except IntegrityError:
                    db.rollback()
        return None


def _renew(lease, ttl):
    now = time.time()
    with Session(engine()) as db:
        changed = db.execute(update(AdmissionLease).where(
            AdmissionLease.scope == lease.scope, AdmissionLease.slot == lease.slot,
            AdmissionLease.owner_token == lease.owner_token, AdmissionLease.expires_at > now
        ).values(expires_at=now + ttl))
        db.commit()
        return changed.rowcount == 1


def _release(owner, scope=None):
    with Session(engine()) as db:
        where = [AdmissionLease.owner_token == owner]
        if scope is not None:
            where.append(AdmissionLease.scope == scope)
        db.execute(update(AdmissionLease).where(*where).values(owner_token=None, expires_at=0))
        db.commit()


async def notify_wait(callback):
    if callback is not None:
        result = callback({"state": "queued", "message": "当前请求较多，正在排队等待可用资源…"})
        if inspect.isawaitable(result):
            await result


def notify_wait_sync():
    callback = _progress.get()
    if callback is None or inspect.iscoroutinefunction(callback) or inspect.iscoroutinefunction(
        getattr(callback, "__call__", None)
    ):
        return
    result = callback({"state": "queued", "message": "当前请求较多，正在排队等待可用资源…"})
    # An unusual synchronous adapter can return a coroutine; do not leak an unawaited object.
    if inspect.iscoroutine(result):
        result.close()


async def drain(task):
    """Finish a cancelled heartbeat despite repeated native task cancellation."""
    if task is None:
        return
    task.cancel()
    with anyio.CancelScope(shield=True):
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()


@asynccontextmanager
async def slot(scope, capacity, queue_capacity, queue_timeout, *, on_wait=None):
    cfg = settings()
    owner, heartbeat = str(uuid.uuid4()), None
    ttl, lost = cfg.admission_lease_seconds, []
    try:
        lease = await run_db(_claim, scope, capacity, owner, ttl)
        if lease is None:
            waiting = await run_db(_claim, scope + ":queue", queue_capacity, owner, queue_timeout + ttl)
            if waiting is None:
                raise AdmissionError("queue_full", "等待队列已满，请稍后再试。")
            await notify_wait(on_wait)
            deadline = time.monotonic() + queue_timeout
            while lease is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AdmissionError("queue_timeout", "排队等待超时，请稍后重新发起请求。", 503)
                await asyncio.sleep(min(cfg.admission_poll_seconds, remaining))
                lease = await run_db(_claim, scope, capacity, owner, ttl)
            await run_db(_release, owner, scope + ":queue")
        holder = asyncio.current_task()

        async def renew():
            try:
                while True:
                    await asyncio.sleep(ttl / 3)
                    if not await run_db(_renew, lease, ttl):
                        raise AdmissionError("lease_lost", "执行资源租约已失效，请重新发起请求。", 503)
            except asyncio.CancelledError:
                raise
            except Exception:
                lost.append(AdmissionError("lease_lost", "执行资源租约已失效，请重新发起请求。", 503))
                holder.cancel()

        heartbeat = asyncio.create_task(renew())
        try:
            yield lease
        except asyncio.CancelledError:
            if lost:
                raise lost[0] from None
            raise
    finally:
        with anyio.CancelScope(shield=True):
            try:
                await drain(heartbeat)
            finally:
                # Also releases a slot claimed just before acquire was cancelled.
                await run_db(_release, owner)


@asynccontextmanager
async def model_slot(profile_id, *, on_wait=None):
    if profile_id is None:
        yield None
        return
    cfg = settings()
    async with slot(f"model:{profile_id}", cfg.model_concurrency, cfg.model_queue_capacity,
                    cfg.model_queue_timeout, on_wait=on_wait or _progress.get()) as lease:
        yield lease


@asynccontextmanager
async def user_slot(user_id, *, on_wait=None):
    cfg = settings()
    async with slot(f"user:{user_id}", cfg.user_chat_concurrency, cfg.user_chat_queue_capacity,
                    cfg.user_chat_queue_timeout, on_wait=on_wait) as lease:
        yield lease


@contextmanager
def model_slot_sync(profile_id):
    """Synchronous generation and embedding share the same slots as streaming requests."""
    check_worker_cancelled()
    if profile_id is None:
        yield None
        return
    cfg = settings()
    scope, owner = f"model:{profile_id}", str(uuid.uuid4())
    ttl, stop, lost, heartbeat = cfg.admission_lease_seconds, threading.Event(), [], None
    cancelled = worker_cancellation.get()
    try:
        lease = _claim(scope, cfg.model_concurrency, owner, ttl)
        if lease is None:
            if _claim(scope + ":queue", cfg.model_queue_capacity, owner, cfg.model_queue_timeout + ttl) is None:
                raise AdmissionError("queue_full", "等待队列已满，请稍后再试。")
            notify_wait_sync()
            deadline = time.monotonic() + cfg.model_queue_timeout
            while lease is None:
                check_worker_cancelled()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AdmissionError("queue_timeout", "排队等待超时，请稍后重新发起请求。", 503)
                delay = min(cfg.admission_poll_seconds, remaining)
                if cancelled is None:
                    time.sleep(delay)
                else:
                    cancelled.wait(delay)
                check_worker_cancelled()
                lease = _claim(scope, cfg.model_concurrency, owner, ttl)
            _release(owner, scope + ":queue")
        check_worker_cancelled()

        def renew():
            while not stop.wait(ttl / 3):
                try:
                    if not _renew(lease, ttl):
                        raise RuntimeError("lease lost")
                except Exception:
                    lost.append(True)
                    return

        heartbeat = threading.Thread(target=renew, name="model-admission-lease", daemon=True)
        heartbeat.start()
        check_worker_cancelled()
        yield lease
        if lost:
            raise AdmissionError("lease_lost", "执行资源租约已失效，请重新发起请求。", 503)
    finally:
        stop.set()
        if heartbeat is not None:
            heartbeat.join()
        _release(owner)
