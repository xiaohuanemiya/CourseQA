import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services import agents, providers, skills
from app.services.retrieval import Evidence


def runtime():
    app = SimpleNamespace(id="app", version=1, published={
        "agent": {"enabled": True, "skills": [], "timeout_seconds": 15},
        "skill_snapshot": skills.snapshot_skills([]),
    })
    profile = SimpleNamespace(provider="openai", model="test", base_url="http://test-model",
                              secret="", temperature=0)
    return agents.AgentRuntime(None, app, {}, Evidence(), profile,
                               SimpleNamespace(message="question"), {})


async def consume(source):
    return [event async for event in source]


def test_full_queue_translated_native_cancellation_still_reaches_consumer():
    async def check():
        runner = runtime()
        failed = asyncio.Event()
        cancellation_counts = []

        async def fail(*args):
            # A model-slot heartbeat cancels its holder and translates that native
            # cancellation into HTTPException while the downstream client is alive.
            for _ in range(runner.queue.maxsize):
                await runner.queue.put(("delta", {"text": "buffered"}))
            holder = asyncio.current_task()
            holder.cancel()
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                cancellation_counts.append(holder.cancelling())
                failed.set()
                raise HTTPException(503, "lease lost") from None

        runner.loop = fail
        source = runner.events()
        assert (await anext(source))[0] == "agent"
        await asyncio.wait_for(failed.wait(), 2)
        assert runner.queue.full() and cancellation_counts == [1]
        delivered = []

        async def collect():
            async for event in source:
                delivered.append(event)

        with pytest.raises(HTTPException, match="lease lost"):
            await asyncio.wait_for(collect(), 2)
        assert len(delivered) == runner.queue.maxsize
        assert all(event[0] == "delta" for event in delivered)
        assert runner.events_closed

    asyncio.run(check())


def install_delta_stream(monkeypatch, runner, *, total=None):
    produced, blocked, closed = [], asyncio.Event(), asyncio.Event()

    async def upstream(*args):
        try:
            yield "delta", '{"status":"insufficient_evidence","answer":"'
            while total is None or len(produced) < total:
                produced.append(len(produced))
                if len(produced) == runner.queue.maxsize + 1:
                    blocked.set()
                yield "delta", "x"
            yield "delta", '","citation_ids":[]}'
            yield "done", {"message": {"role": "assistant", "tool_calls": [],
                "content": json.dumps({"status": "insufficient_evidence", "answer": "x" * total,
                                       "citation_ids": []})},
                "usage": {"prompt_tokens": 1, "completion_tokens": total}}
        finally:
            closed.set()

    async def loop(*args):
        await runner.turn("main", [], [])

    async def current():
        pass

    monkeypatch.setattr(providers, "stream_tool_turn", upstream)
    runner.loop, runner.check_current = loop, current
    return produced, blocked, closed


def test_slow_consumer_bounds_delta_read_ahead_and_preserves_complete_answer(monkeypatch):
    async def check():
        runner = runtime()
        produced, blocked, closed = install_delta_stream(monkeypatch, runner, total=1024)
        source = runner.events()
        assert (await anext(source))[0] == "agent"
        await asyncio.wait_for(blocked.wait(), 2)
        await asyncio.sleep(0)
        assert runner.queue.maxsize == 128
        assert runner.queue.qsize() == 128 and len(produced) == 129
        first = await anext(source)
        await asyncio.sleep(0)
        assert first == ("delta", {"text": "x"})
        assert runner.queue.qsize() == 128 and len(produced) == 130
        remaining = await asyncio.wait_for(consume(source), 2)
        text = first[1]["text"] + "".join(value["text"] for kind, value in remaining if kind == "delta")
        assert text == runner.result["answer"] == "x" * 1024
        assert closed.is_set()

    asyncio.run(check())


def test_closing_consumer_cancels_full_queue_producer_and_closes_upstream(monkeypatch):
    async def check():
        runner = runtime()
        produced, blocked, closed = install_delta_stream(monkeypatch, runner)
        source = runner.events()
        assert (await anext(source))[0] == "agent"
        await asyncio.wait_for(blocked.wait(), 2)
        assert runner.queue.full() and len(produced) == 129
        await asyncio.wait_for(source.aclose(), 2)
        assert runner.events_closed and closed.is_set()
        assert len(produced) == 129
        assert runner.model_turns[0]["state"] == "cancelled"

    asyncio.run(check())
