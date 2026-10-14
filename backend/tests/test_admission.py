import asyncio
import concurrent.futures
import multiprocessing
import threading
import time

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.models import AdmissionLease
from app.services import admission
from . import test_agent_evaluation

platform = test_agent_evaluation.platform  # Reuse the offline SQLite fixture without starting HTTP.


def active():
    with Session(engine()) as db:
        return db.scalars(select(AdmissionLease).where(AdmissionLease.owner_token.is_not(None))).all()


def _other_process_claim(output):
    output.put(admission._claim("process-scope", 1, "child", 30) is not None)


def test_slots_are_shared_between_processes(platform):
    assert admission._claim("process-scope", 1, "parent", 30)
    context = multiprocessing.get_context("spawn")
    output = context.Queue()
    process = context.Process(target=_other_process_claim, args=(output,))
    process.start()
    try:
        assert output.get(timeout=15) is False
        process.join(timeout=5)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        output.close()
        admission._release("parent")


def test_parallel_claims_are_atomic_and_expired_owners_cannot_release_successors(platform):
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda index: admission._claim("scope", 3, str(index), 30), range(8)))
    assert len([item for item in claims if item is not None]) == 3
    assert len(active()) == 3
    old = admission._claim("expired", 1, "old", -.1)
    replacement = admission._claim("expired", 1, "new", 30)
    assert replacement and not admission._renew(old, 30)
    admission._release("old")
    assert admission._claim("expired", 1, "third", 30) is None


def test_async_and_sync_callers_share_limit_and_waiters_receive_progress(platform, monkeypatch):
    cfg = settings()
    monkeypatch.setattr(cfg, "model_concurrency", 1)
    monkeypatch.setattr(cfg, "admission_poll_seconds", .01)
    held, release, order = threading.Event(), threading.Event(), []

    def sync_work():
        with admission.model_slot_sync("profile"):
            order.append("sync_start")
            held.set()
            assert release.wait(3)
            order.append("sync_end")

    async def check():
        waiting = asyncio.Event()

        async def async_work():
            async with admission.model_slot("profile", on_wait=lambda _: waiting.set()):
                order.append("async_start")

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(sync_work)
            assert held.wait(2)
            task = asyncio.create_task(async_work())
            try:
                await asyncio.wait_for(waiting.wait(), 2)
                assert order == ["sync_start"]
            finally:
                release.set()
            await asyncio.wait_for(task, 2)
            running.result(timeout=2)

    asyncio.run(check())
    assert order == ["sync_start", "sync_end", "async_start"]
    assert not active()


def test_queue_capacity_timeout_and_cancellation_release_all_leases(platform, monkeypatch):
    cfg = settings()
    monkeypatch.setattr(cfg, "model_concurrency", 1)
    monkeypatch.setattr(cfg, "model_queue_capacity", 1)
    monkeypatch.setattr(cfg, "model_queue_timeout", .1)
    monkeypatch.setattr(cfg, "admission_poll_seconds", .01)

    async def check():
        waiting = asyncio.Event()

        async def wait():
            async with admission.model_slot("profile", on_wait=lambda _: waiting.set()):
                pytest.fail("must not acquire occupied slot")

        async with admission.model_slot("profile"):
            task = asyncio.create_task(wait())
            await asyncio.wait_for(waiting.wait(), 2)
            with pytest.raises(admission.AdmissionError) as error:
                async with admission.model_slot("profile"):
                    pass
            assert error.value.code == "queue_full"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert len(active()) == 1
            with pytest.raises(admission.AdmissionError) as error:
                await wait()
            assert error.value.code == "queue_timeout"
            assert len(active()) == 1

    asyncio.run(check())
    assert not active()


def test_cancel_after_database_claim_does_not_leak_slot(platform, monkeypatch):
    real_claim = admission._claim

    async def check():
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()

        def delayed(*args):
            result = real_claim(*args)
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(2)
            return result

        monkeypatch.setattr(admission, "_claim", delayed)

        async def operation():
            async with admission.model_slot("profile"):
                pytest.fail("cancelled request must not execute")

        task = asyncio.create_task(operation())
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)

    asyncio.run(check())
    assert not active()


def test_heartbeat_renews_long_running_sync_lease(platform, monkeypatch):
    cfg = settings()
    monkeypatch.setattr(cfg, "admission_lease_seconds", 3)
    monkeypatch.setattr(cfg, "model_concurrency", 1)
    with admission.model_slot_sync("profile"):
        time.sleep(3.1)
        assert admission._claim("model:profile", 1, "intruder", 30) is None
    assert not active()


def test_standalone_provider_without_profile_id_does_not_touch_database(monkeypatch):
    monkeypatch.setattr(admission, "engine", lambda: pytest.fail("must not open a database"))

    async def check():
        async with admission.model_slot(None):
            pass

    asyncio.run(check())
    with admission.model_slot_sync(None):
        pass


def test_sync_queue_notifies_context_callback_once_and_ignores_async_callbacks(platform, monkeypatch):
    cfg = settings()
    monkeypatch.setattr(cfg, "model_concurrency", 1)
    monkeypatch.setattr(cfg, "model_queue_timeout", .03)
    monkeypatch.setattr(cfg, "admission_poll_seconds", .01)
    assert admission._claim("model:profile", 1, "held", 30)
    notifications = []
    token = admission.set_progress(notifications.append)
    try:
        with pytest.raises(admission.AdmissionError) as error:
            with admission.model_slot_sync("profile"):
                pass
        assert error.value.code == "queue_timeout"
    finally:
        admission.reset_progress(token)
        admission._release("held")
    assert len(notifications) == 1 and notifications[0]["state"] == "queued"

    async def callback(_):
        pytest.fail("synchronous entry must not call an async callback")

    token = admission.set_progress(callback)
    try:
        admission.notify_wait_sync()
    finally:
        admission.reset_progress(token)
