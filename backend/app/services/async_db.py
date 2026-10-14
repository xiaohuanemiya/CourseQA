"""Keep a synchronous database operation alive until its worker thread has stopped."""
import asyncio
import threading
from contextvars import ContextVar

import anyio
from starlette.concurrency import run_in_threadpool


# Each thread operation gets its own signal. Cancelling a heartbeat must not
# cancel model work belonging to another operation in the same request.
worker_cancellation = ContextVar("db_worker_cancellation", default=None)


async def run_db(function, *args, **kwargs):
    """Delay cancellation until the worker exits, preserving caller-held session locks.

    Cancelling an asyncio task cannot stop its worker thread. Awaiting the shielded
    worker before propagating cancellation prevents a caller from releasing its DB
    lock, rolling back, or closing the session while that thread still uses it.
    """
    cancelled = threading.Event()

    async def invoke():
        token = worker_cancellation.set(cancelled)
        try:
            # Shield against enclosing AnyIO scopes as well as native task cancellation.
            with anyio.CancelScope(shield=True):
                return await run_in_threadpool(function, *args, **kwargs)
        finally:
            worker_cancellation.reset(token)

    worker = asyncio.create_task(invoke())
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        # Cooperative provider admission can stop queued work before sending HTTP.
        # Ordinary DB operations still finish before their session/lock is released.
        cancelled.set()
        with anyio.CancelScope(shield=True):
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    # Disconnect and timeout may both cancel the same task during cleanup.
                    continue
                except Exception:
                    break
            # Retrieve any worker failure, but preserve the caller's cancellation.
            if not worker.cancelled():
                worker.exception()
        raise
