import asyncio
import threading
from types import SimpleNamespace

import anyio
import pytest

from app.services.async_db import run_db


@pytest.mark.parametrize("failure", [False, True])
def test_repeated_task_cancellation_keeps_lock_until_thread_finishes(failure):
    async def check():
        loop = asyncio.get_running_loop()
        entered, release, finished = asyncio.Event(), threading.Event(), threading.Event()
        lock, order = asyncio.Lock(), []

        def work():
            loop.call_soon_threadsafe(entered.set)
            try:
                assert release.wait(2)
                if failure:
                    raise ValueError("worker failed after cancellation")
            finally:
                order.append("thread_finished")
                finished.set()

        async def first():
            async with lock:
                await run_db(work)

        async def second():
            async with lock:
                assert finished.is_set()
                order.append("next_session_access")

        task = asyncio.create_task(first())
        await asyncio.wait_for(entered.wait(), 2)
        next_task = asyncio.create_task(second())
        try:
            task.cancel()
            await asyncio.sleep(.01)
            task.cancel()
            await asyncio.sleep(.01)
            assert not task.done() and lock.locked()
            assert not next_task.done() and not finished.is_set()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        await asyncio.wait_for(next_task, 2)
        assert order == ["thread_finished", "next_session_access"]

    asyncio.run(check())


def test_anyio_scope_cancellation_also_waits_for_worker():
    async def check():
        loop = asyncio.get_running_loop()
        entered, release, finished = asyncio.Event(), threading.Event(), threading.Event()
        scopes, lock = [], asyncio.Lock()

        def work():
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(2)
            finished.set()

        async def operation():
            with anyio.CancelScope() as scope:
                scopes.append(scope)
                async with lock:
                    await run_db(work)
            assert finished.is_set()

        task = asyncio.create_task(operation())
        await asyncio.wait_for(entered.wait(), 2)
        try:
            scopes[0].cancel()
            await asyncio.sleep(.01)
            assert not task.done() and lock.locked()
        finally:
            release.set()
        await asyncio.wait_for(task, 2)

    asyncio.run(check())


def test_worker_result_and_error_are_preserved_without_cancellation():
    async def check():
        assert await run_db(lambda value, increment: value + increment, 3, increment=4) == 7

        def failure():
            raise ValueError("database operation failed")

        with pytest.raises(ValueError, match="database operation failed"):
            await run_db(failure)

    asyncio.run(check())


def test_cancel_during_commit_waits_before_session_close_and_does_not_record_failure(monkeypatch):
    from app.services import agents, learning, streaming

    async def check():
        loop = asyncio.get_running_loop()
        entered, release, committed = asyncio.Event(), threading.Event(), threading.Event()
        failures, closed = [], []
        result = {"status": "insufficient_evidence", "answer": "无证据", "citations": []}

        class FakeSession:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def commit(self):
                pass

            def __exit__(self, *args):
                assert committed.is_set()
                closed.append(True)

        def save(*args, **kwargs):
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(2)
            committed.set()
            return result

        monkeypatch.setattr(streaming, "Session", FakeSession)
        monkeypatch.setattr(streaming, "engine", lambda: None)
        monkeypatch.setattr(learning, "prepare_chat", lambda *args, **kwargs: (
            SimpleNamespace(), {}, None, [], SimpleNamespace(provider="demo"), result))
        monkeypatch.setattr(learning, "save_chat", save)
        monkeypatch.setattr(agents, "should_run", lambda *args: False)
        monkeypatch.setattr(agents, "record_failure", lambda *args: failures.append(True))
        stream = streaming._answer_events("app", "user", SimpleNamespace(calculation=None))
        assert (await anext(stream))[0] == "status"
        assert (await anext(stream))[0] == "delta"
        assert (await anext(stream))[0] == "status"
        task = asyncio.create_task(anext(stream))
        await asyncio.wait_for(entered.wait(), 2)
        try:
            task.cancel()
            await asyncio.sleep(.01)
            assert not task.done() and not closed
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert committed.is_set() and closed and not failures
        await stream.aclose()

    asyncio.run(check())
