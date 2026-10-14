import asyncio
import json
import threading

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import engine
from app.models import Knowledge, User
from app.schemas import ChatInput
from app.services import agents, learning
from .conftest import post
from .test_agents import answer_reply, call, enable, reply, streamer
from .test_streaming import events


def test_repeated_read_only_tools_reuse_results_but_reserve_each_request(client, platform, monkeypatch):
    app_id, _ = enable(platform)
    operations = []
    original = agents.AgentRuntime.execute

    async def execute(self, name, args, agent_id):
        operations.append(name)
        return await original(self, name, args, agent_id)

    async def planner(profile, messages, definitions):
        if len(messages) == 2:
            return reply(call('read_skill', {'name': 'course-evidence'}, 'a'),
                         call('read_skill', {'name': 'course-evidence'}, 'b'))
        assert json.loads(messages[-1]['content']) == json.loads(messages[-2]['content'])
        return answer_reply()

    monkeypatch.setattr(agents.AgentRuntime, 'execute', execute)
    streamer(monkeypatch, planner)
    result = post(client, f'/apps/{app_id}/chat', {'message': '导数知识'})
    assert operations == ['read_skill']
    assert result['agent']['tool_calls'] == 2
    assert result['agent']['tool_executions'] == result['agent']['cache_hits'] == 1


def test_concurrent_children_share_identical_calculation(client, platform, monkeypatch):
    app_id, _ = enable(platform)
    invoked, both = threading.Event(), asyncio.Event()
    calculations, child_turns = [], []

    def calculate(operation, payload):
        calculations.append(payload)
        invoked.set()
        assert release.wait(5)
        return {'status': 'computed', 'result': '2*x'}

    release = threading.Event()

    async def planner(profile, messages, definitions):
        task = json.loads(messages[1]['content'])['task']
        if task == '导数知识':
            if len(messages) == 2:
                return reply(*(call('spawn_agent', {'name': task, 'task': task}, task) for task in ('a', 'b')))
            return answer_reply()
        if len(messages) == 2:
            child_turns.append(task)
            if len(child_turns) == 2:
                both.set()
            await both.wait()
            return reply(call('calculate', {'operation': 'differentiate', 'expression': 'x^2'}))
        return reply(content='报告 [1]')

    async def trigger_release():
        while not invoked.is_set():
            await asyncio.sleep(.01)
        await asyncio.sleep(.05)
        release.set()

    monkeypatch.setattr(learning, 'bounded', calculate)
    streamer(monkeypatch, planner)

    async def check():
        worker = asyncio.create_task(trigger_release())
        with Session(engine()) as db:
            user = db.scalar(select(User).where(User.username == 'admin'))
            data = ChatInput(message='导数知识')
            app, versions, _, evidence, profile, result = learning.prepare_chat(db, app_id, user.id, data)
            runtime = agents.AgentRuntime(db, app, versions, evidence, profile, data, result)
            try:
                await asyncio.wait_for(agents.prepare(runtime), 8)
            finally:
                release.set()
                await worker
        assert len(calculations) == 1
        assert result['agent']['cache_hits'] == 1
        assert result['agent']['tool_calls'] == 4

    asyncio.run(check())


def test_cache_does_not_allow_withdrawn_evidence(client, platform, monkeypatch):
    app_id, _ = enable(platform)
    original = agents.AgentRuntime.reusable_tool
    hits = []

    async def cached(self, name, args, agent_id):
        value = await original(self, name, args, agent_id)
        hits.append(name)
        if len(hits) == 1:
            with Session(engine()) as db:
                db.get(Knowledge, platform['bundles'][0]['node']['id']).withdrawn = True
                db.commit()
        return value

    async def planner(*args):
        return reply(call('read_skill', {'name': 'course-evidence'}, 'a'),
                     call('read_skill', {'name': 'course-evidence'}, 'b'))

    monkeypatch.setattr(agents.AgentRuntime, 'reusable_tool', cached)
    streamer(monkeypatch, planner)
    frames = events(client.post(f'/api/apps/{app_id}/chat/stream', json={'message': '导数知识'}))
    assert frames[-1][0] == 'error' and frames[-1][1]['code'] == 409
    assert hits == ['read_skill']
    assert client.get(f'/api/apps/{app_id}/conversations').json() == []


@pytest.mark.parametrize('status', ['timeout', 'pending'])
def test_unsolved_calculations_are_not_cached(client, platform, monkeypatch, status):
    app_id, _ = enable(platform)
    performed = []

    def calculate(*args):
        performed.append(True)
        return {'status': status}

    async def planner(profile, messages, definitions):
        if len(messages) == 2:
            args = {'operation': 'integrate', 'expression': 'x'}
            return reply(call('calculate', args, 'a'), call('calculate', args, 'b'))
        return answer_reply()

    monkeypatch.setattr(learning, 'bounded', calculate)
    streamer(monkeypatch, planner)
    result = post(client, f'/apps/{app_id}/chat', {'message': '导数知识'})
    assert len(performed) == 2
    assert result['agent']['cache_hits'] == 0
