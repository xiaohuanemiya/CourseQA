import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import engine
from app.models import Application, Audit, Knowledge, ModelProfile, User
from app.schemas import AgentOptions, ChatInput
from app.services import agents, learning, providers, skills, streaming
from .conftest import post
from .test_document_retrieval import PAPER, QUERY, document, publish
from .test_streaming import events


def call(name, arguments, key="call-1"):
    return {"id": key, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def reply(*calls, content="资料准备完成 [1]。"):
    return {"role": "assistant", "content": content, "tool_calls": list(calls),
            "reasoning_content": "private reasoning must not be shown"}


def enable(platform, **options):
    bundle = platform["bundles"][0]
    with Session(engine()) as db:
        app = db.get(Application, bundle["app"]["id"])
        configuration = AgentOptions(enabled=True, **options)
        app.published = {**app.published, "agent": configuration.model_dump(),
                         "skill_snapshot": skills.snapshot_skills(configuration.skills)}
        profile = db.get(ModelProfile, platform["generation"]["id"])
        profile.provider, profile.base_url = "openai", "http://test-model"
        user_id = db.scalar(select(User.id).where(User.username == "admin"))
        db.commit()
    return bundle["app"]["id"], user_id


def answer_reply(**changes):
    value = {"status": "answered", "answer": "导数知识依据 [1]。", "citation_ids": ["1"], **changes}
    return reply(content=json.dumps(value, ensure_ascii=False))


def streamer(monkeypatch, planner):
    async def upstream(profile, messages, definitions):
        message = await planner(profile, messages, definitions)
        content = message["content"]
        for start in range(0, len(content), 7):
            yield "delta", content[start:start + 7]
        yield "done", {"message": message, "usage": {"prompt_tokens": 23, "completion_tokens": 11}}

    monkeypatch.setattr(providers, "stream_tool_turn", upstream)
    monkeypatch.setattr(learning, "generate", lambda *a, **kw: pytest.fail("Unexpected second model request"))
    monkeypatch.setattr(streaming, "stream_generate", lambda *a, **kw: pytest.fail("Unexpected second model stream"))


@pytest.mark.parametrize("provider", ["openai", "ollama"])
def test_native_tool_protocol_preserves_call_results_and_private_reasoning(monkeypatch, provider):
    captured = []
    tool_call = call("calculate", {"operation": "differentiate", "expression": "x^2"})
    if provider == "ollama":
        tool_call["function"]["arguments"] = json.loads(tool_call["function"]["arguments"])
        del tool_call["id"]

    def handler(request):
        assert "authorization" not in request.headers
        captured.append(json.loads(request.content))
        message = reply(tool_call) if len(captured) == 1 else reply(content="完成")
        if provider == "ollama":
            body = {"message": message, "done": True, "done_reason": "stop"}
        else:
            body = {"choices": [{"finish_reason": "tool_calls" if len(captured) == 1 else "stop", "message": message}]}
        return httpx.Response(200, json=body)

    original = httpx.AsyncClient
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    profile = SimpleNamespace(provider=provider, base_url="http://local", model="test", secret="", temperature=.1)

    async def check():
        messages = [{"role": "user", "content": "计算导数"}]
        toolset = agents.tool_definitions({}, True)
        message = await providers.tool_turn(profile, messages, toolset)
        messages.extend([message, providers.tool_result_message(profile, message["tool_calls"][0], {"result": "2*x"})])
        await providers.tool_turn(profile, messages, toolset)

    asyncio.run(check())
    assert "max_tokens" not in captured[0] and "response_format" not in captured[0]
    assert captured[0]["tools"] and captured[1]["messages"][1]["reasoning_content"]
    result = captured[1]["messages"][2]
    if provider == "openai":
        assert result["tool_call_id"] == "call-1"
    else:
        assert result["tool_name"] == "calculate" and captured[0]["options"]["num_predict"] == -1


@pytest.mark.parametrize("body,status", [
    ({"choices": [{"finish_reason": "length", "message": reply()}]}, 200),
    ({"choices": [{"finish_reason": "tool_calls", "message": reply()}]}, 200),
    ({"choices": [{"finish_reason": "tool_calls", "message": reply(call("x", {}), call("x", {}))}]}, 200),
    ({"secret": "DO_NOT_EXPOSE"}, 400),
    ({"secret": "DO_NOT_EXPOSE"}, 500),
])
def test_tool_provider_rejects_bad_responses_without_exposing_body(monkeypatch, body, status):
    original = httpx.AsyncClient
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **kw: original(
        transport=httpx.MockTransport(lambda _: httpx.Response(status, json=body)), **kw))
    profile = SimpleNamespace(provider="openai", base_url="http://local", model="test", secret="", temperature=.1)
    with pytest.raises(providers.ProviderError) as error:
        asyncio.run(providers.tool_turn(profile, [], []))
    assert "DO_NOT_EXPOSE" not in str(error.value)


@pytest.mark.parametrize("stream", [False, True])
def test_model_selects_skills_tools_and_isolated_child_then_saves_trace(client, platform, monkeypatch, stream):
    app_id, _ = enable(platform)
    seen = []

    async def planner(profile, messages, definitions):
        task = json.loads(messages[1]["content"])["task"]
        tools = {d["function"]["name"] for d in definitions}
        seen.append((task, len(messages)))
        if task == "核查求导":
            assert "spawn_agent" not in tools
            if len(messages) == 2:
                return reply(call("calculate", {"operation": "differentiate", "expression": "x^2"}))
            assert json.loads(messages[-1]["content"])["result"] == "2*x"
            return reply(content='{"report":"求导工具得到 2*x，教学依据 [1]。"}')
        if len(messages) == 2:
            return reply(call("read_skill", {"name": "calculus-verification"}))
        if len(messages) == 4:
            assert "不定积分" in json.loads(messages[-1]["content"])["instructions"]
            return reply(call("spawn_agent", {"name": "计算核查", "task": "核查求导"}))
        report = json.loads(next(m["content"] for m in reversed(messages) if m["role"] == "tool"))
        assert report["name"] == "计算核查" and "2*x" in report["report"]
        refreshed = json.loads(messages[-1]["content"])
        assert refreshed["calculations"][0]["output"]["result"] == "2*x"
        assert refreshed["evidence"][0]["citation_id"] == "1"
        return answer_reply()

    streamer(monkeypatch, planner)
    response = client.post(f"/api/apps/{app_id}/chat" + ("/stream" if stream else ""), json={"message": "导数知识"})
    if stream:
        frames = events(response)
        assert frames[-1][0] == "done", frames
        result = frames[-1][1]
        assert any(k == "agent" and v["agent_id"] == "child-1" for k, v in frames)
        assert sum(k == "delta" for k, _ in frames) > 1
    else:
        assert response.status_code == 200, response.text
        result = response.json()
    assert result["agent"]["tool_calls"] == 3 and result["agent"]["subagents"] == 1
    assert result["agent"]["model_calls"] == 5
    assert result["agent"]["skills"][0]["name"] == "calculus-verification"
    assert result["calculation"]["result"] == "2*x"
    assert "private reasoning" not in json.dumps(result)
    history = client.get(f"/api/apps/{app_id}/conversations/{result['conversation_id']}").json()
    assert history[0]["result"] == result
    assert len(seen) == 5


@pytest.mark.parametrize("stream", [False, True])
def test_model_can_answer_in_one_call_without_spawning_or_using_tools(client, platform, monkeypatch, stream):
    app_id, _ = enable(platform)
    async def planner(*args):
        return answer_reply()
    streamer(monkeypatch, planner)
    if stream:
        frames = events(client.post(f"/api/apps/{app_id}/chat/stream", json={"message": "导数知识"}))
        assert frames[-1][0] == "done", frames
        result = frames[-1][1]
        assert "".join(value["text"] for kind, value in frames if kind == "delta") == result["answer"]
    else:
        result = post(client, f"/apps/{app_id}/chat", {"message": "导数知识"})
    assert result["agent"]["subagents"] == result["agent"]["tool_calls"] == 0
    assert result["agent"]["model_calls"] == 1
    assert result["agent"]["usage"] == {"prompt_tokens": 23, "completion_tokens": 11}


@pytest.mark.parametrize("tool,arguments,code", [
    ("run_shell", {"command": "cat .env"}, "tool_not_allowed"),
    ("calculate", {"operation": "execute", "expression": "x"}, "invalid_arguments"),
    ("search_knowledge", {"query": "积分", "kb_ids": ["other"]}, "invalid_arguments"),
    ("read_skill", {"name": "../../.env"}, "skill_disabled"),
    ("read_skill_reference", {"name": "course-evidence", "path": "../../.env"}, "skill_resource_unavailable"),
    ("read_skill", {"name": "sympy"}, "skill_disabled"),
    ("spawn_agent", {"name": "child", "task": "无限递归"}, "tool_not_allowed"),
])
def test_invalid_or_disabled_tools_are_reported_to_model_without_execution(
        client, platform, monkeypatch, tool, arguments, code):
    app_id, _ = enable(platform, max_subagents=0, skills=["course-evidence"])
    async def planner(profile, messages, definitions):
        if len(messages) == 2:
            return reply(call(tool, arguments))
        error = json.loads(next(m["content"] for m in reversed(messages) if m["role"] == "tool"))["error"]
        assert error["code"] == code and error["retryable"] is False and error["message"]
        return answer_reply()
    streamer(monkeypatch, planner)
    result = post(client, f"/apps/{app_id}/chat", {"message": "导数知识"})
    assert result["agent"]["subagents"] == 0
    assert any(e["kind"] == "tool_failed" and e["error_code"] == code for e in result["agent"]["trace"])


def test_child_cannot_recursively_spawn_and_total_budget_stops_loop(client, platform, monkeypatch):
    app_id, _ = enable(platform, max_tool_calls=3)
    turns = []
    async def planner(profile, messages, definitions):
        turns.append(len(messages))
        task = json.loads(messages[1]["content"])["task"]
        if task == "child-task":
            assert "spawn_agent" not in {d["function"]["name"] for d in definitions}
        if not definitions:
            return answer_reply() if task != "child-task" else reply(content='{"report":"预算已用尽，未创建更多子任务。"}')
        return reply(call("spawn_agent", {"name": "child", "task": "child-task"}))
    streamer(monkeypatch, planner)
    result = post(client, f"/apps/{app_id}/chat", {"message": "导数知识"})
    assert result["agent"]["tool_calls"] == 3
    assert result["agent"]["model_calls"] == len(turns)
    assert result["agent"]["limited"] and result["agent"]["subagents"] == 1
    assert any(event.get("error_code") == "recursive_spawn_forbidden" for event in result["agent"]["trace"])


def test_agent_search_keeps_original_document_scope_and_app_binding(client, platform, monkeypatch):
    a, b = platform["bundles"]
    doc_id, own = document(a, PAPER, [("求导方法", "导数知识：幂函数可用求导法则。")])
    document(a, "2022-2023一元B上月考.pdf", [("求导方法", "其他年份的不可使用内容")])
    document(b, PAPER, [("求导方法", "其他应用的秘密")])
    publish(client, a)
    publish(client, b)
    app_id, _ = enable(platform)

    async def planner(profile, messages, definitions):
        if len(messages) == 2:
            return reply(call("search_knowledge", {"query": "求导方法"}))
        result = json.loads(messages[-1]["content"])
        assert result["evidence"]
        assert all(d["id"] == doc_id for e in result["evidence"] for d in e["documents"])
        assert "不可使用" not in messages[-1]["content"] and "秘密" not in messages[-1]["content"]
        return answer_reply()

    streamer(monkeypatch, planner)
    result = post(client, f"/apps/{app_id}/chat", {"message": QUERY})
    assert result["agent"]["evidence_ids"] == own


@pytest.mark.parametrize("failure", ["withdrawn", "version", "provider", "citation", "timeout"])
def test_agent_failures_do_not_save_a_conversation(client, platform, monkeypatch, failure):
    app_id, _ = enable(platform)
    async def planner(*args):
        if failure == "provider":
            raise providers.ProviderError("模型失败")
        if failure == "timeout":
            raise TimeoutError()
        with Session(engine()) as db:
            if failure == "withdrawn":
                db.get(Knowledge, platform["bundles"][0]["node"]["id"]).withdrawn = True
            if failure == "version":
                db.get(Application, app_id).version += 1
            db.commit()
        if failure == "citation":
            return answer_reply(answer="伪造 [999]", citation_ids=["999"])
        return answer_reply()
    streamer(monkeypatch, planner)
    frames = events(client.post(f"/api/apps/{app_id}/chat/stream", json={"message": "导数知识"}))
    assert frames[-1][0] == "error", frames
    assert client.get(f"/api/apps/{app_id}/conversations").json() == []
    with Session(engine()) as db:
        audit = db.scalar(select(Audit).where(Audit.entity_id == app_id, Audit.action == "agent_failure"))
        assert audit is not None
        assert audit.after["error_code"] and audit.after["model_calls"] >= 1
        assert audit.after["state"] in {"failed", "timeout"}
        assert "伪造" not in json.dumps(audit.after, ensure_ascii=False)
        assert "private reasoning" not in json.dumps(audit.after)


def test_cancellation_closes_all_active_child_streams_and_does_not_save(client, platform, monkeypatch):
    app_id, user_id = enable(platform)
    closed, running = [], []
    started = asyncio.Event()
    async def upstream(profile, messages, definitions):
        task = json.loads(messages[1]["content"])["task"]
        if not task.startswith("child-task-"):
            message = reply(*[call("spawn_agent", {"name": f"child-{i}", "task": f"child-task-{i}"}, key=f"call-{i}")
                              for i in range(2)])
            yield "done", {"message": message, "usage": {"prompt_tokens": 20, "completion_tokens": 10}}
            return
        try:
            running.append(task)
            if len(running) == 2:
                started.set()
            await asyncio.Event().wait()
        finally:
            closed.append(task)
    monkeypatch.setattr(providers, "stream_tool_turn", upstream)

    async def check():
        stream = streaming.event_stream(streaming.chat_events(app_id, user_id, ChatInput(message="导数知识")), .01)
        async for frame in stream:
            if started.is_set() and "keep-alive" in frame:
                await stream.aclose()
                break
    asyncio.run(asyncio.wait_for(check(), timeout=3))
    assert set(closed) == {"child-task-0", "child-task-1"}
    assert client.get(f"/api/apps/{app_id}/conversations").json() == []
    with Session(engine()) as db:
        audit = db.scalar(select(Audit).where(Audit.entity_id == app_id, Audit.action == "agent_failure"))
        assert audit.after["state"] == audit.after["error_code"] == "cancelled"


def test_independent_children_overlap_but_respect_parallel_and_total_limits(client, platform, monkeypatch):
    app_id, _ = enable(platform, max_parallel_subagents=2, max_subagents=3)
    together = asyncio.Event()
    running, maximum = 0, 0
    started, completed = [], []

    async def planner(profile, messages, definitions):
        nonlocal running, maximum
        task = json.loads(messages[1]["content"])["task"]
        if task.startswith("job-"):
            running += 1
            maximum = max(maximum, running)
            started.append(task)
            try:
                if len(started) <= 2:
                    if len(started) == 2:
                        together.set()
                    # Sequential scheduling cannot reach this barrier and fails.
                    await asyncio.wait_for(together.wait(), timeout=2)
                else:
                    assert completed
                await asyncio.sleep(0)
                return reply(content=json.dumps({"report": task + "已核对 [1]"}))
            finally:
                completed.append(task)
                running -= 1
        if len(messages) == 2:
            return reply(*[call("spawn_agent", {"name": f"子任务 {i}", "task": f"job-{i}"}, key=f"spawn-{i}")
                           for i in range(4)])
        outputs = [json.loads(message["content"]) for message in messages if message["role"] == "tool"]
        assert len([output for output in outputs if "report" in output]) == 3
        assert [output["error"]["code"] for output in outputs if "error" in output] == ["subagent_limit"]
        assert json.loads(messages[-1]["content"])["evidence"][0]["citation_id"] == "1"
        return answer_reply()

    streamer(monkeypatch, planner)
    result = post(client, f"/apps/{app_id}/chat", {"message": "分别核查三个导数知识子问题"})
    assert maximum == 2 and set(started) == {"job-0", "job-1", "job-2"}
    assert result["agent"]["subagents"] == 3 and result["agent"]["max_parallel_subagents"] == 2
    assert result["agent"]["limited"] and result["agent"]["model_calls"] == 5
    active, observed = 0, 0
    for event in result["agent"]["trace"]:
        if event["agent_id"] == "main":
            continue
        active += event["kind"] == "agent_started"
        active -= event["kind"] == "agent_completed"
        observed = max(observed, active)
        assert 0 <= active <= 2
    assert active == 0 and observed == 2


def test_malformed_tool_json_is_actionable_and_model_can_recover(client, platform, monkeypatch):
    app_id, _ = enable(platform)
    malformed = call("calculate", {})
    malformed["function"]["arguments"] = '{"expression":'

    async def planner(profile, messages, definitions):
        if len(messages) == 2:
            return reply(malformed)
        outputs = [json.loads(message["content"]) for message in messages if message["role"] == "tool"]
        assert outputs[0]["error"]["code"] == "invalid_json"
        assert "JSON" in outputs[0]["error"]["message"]
        if len(outputs) == 1:
            return reply(call("calculate", {"operation": "differentiate", "expression": "x^2"}, key="repaired"))
        assert outputs[-1]["result"] == "2*x"
        return answer_reply()

    streamer(monkeypatch, planner)
    result = post(client, f"/apps/{app_id}/chat", {"message": "导数知识"})
    assert result["agent"]["model_calls"] == 3 and result["agent"]["tool_calls"] == 2
    assert result["calculation"]["result"] == "2*x"


def test_failing_child_cancels_waiting_sibling_stream(client, platform, monkeypatch):
    app_id, _ = enable(platform, max_parallel_subagents=2)
    waiting = asyncio.Event()
    closed, cancelled = [], []

    async def upstream(profile, messages, definitions):
        task = json.loads(messages[1]["content"])["task"]
        if task == "导数知识":
            message = reply(call("spawn_agent", {"name": "等待", "task": "waiting"}, key="wait"),
                            call("spawn_agent", {"name": "失败", "task": "failing"}, key="fail"))
            yield "done", {"message": message, "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
            return
        try:
            if task == "waiting":
                waiting.set()
                try:
                    await asyncio.wait_for(asyncio.Event().wait(), timeout=3)
                except asyncio.CancelledError:
                    cancelled.append(task)
                    raise
            else:
                await asyncio.wait_for(waiting.wait(), timeout=2)
                raise providers.ProviderError("子任务模型失败", code="child_provider_failed")
        finally:
            closed.append(task)

    monkeypatch.setattr(providers, "stream_tool_turn", upstream)
    frames = events(client.post(f"/api/apps/{app_id}/chat/stream", json={"message": "导数知识"}))
    assert frames[-1][0] == "error" and frames[-1][1]["error_code"] == "child_provider_failed"
    assert set(closed) == {"waiting", "failing"} and cancelled == ["waiting"]
    assert client.get(f"/api/apps/{app_id}/conversations").json() == []
    with Session(engine()) as db:
        audit = db.scalar(select(Audit).where(Audit.entity_id == app_id, Audit.action == "agent_failure"))
        assert audit.after["error_code"] == "child_provider_failed"
        assert audit.after["subagents"] == 2


@pytest.mark.parametrize("has_delta", [False, True])
def test_retryable_model_failure_retries_once_only_before_any_delta(client, platform, monkeypatch, has_delta):
    app_id, _ = enable(platform)
    attempts = []

    async def upstream(profile, messages, definitions):
        attempts.append(True)
        if len(attempts) == 1:
            if has_delta:
                yield "delta", '{"status":"answered","answer":"已经开始的正文'
            raise providers.ProviderError("模型暂时不可用", code="provider_http_503", retryable=True)
        message = answer_reply()
        yield "delta", message["content"]
        yield "done", {"message": message, "usage": {"prompt_tokens": 23, "completion_tokens": 11}}

    monkeypatch.setattr(providers, "stream_tool_turn", upstream)
    frames = events(client.post(f"/api/apps/{app_id}/chat/stream", json={"message": "导数知识"}))
    if has_delta:
        assert len(attempts) == 1
        assert any(kind == "delta" for kind, _ in frames)
        assert not any(kind == "agent" and value["kind"] == "model_retry" for kind, value in frames)
        assert frames[-1][0] == "error" and frames[-1][1]["error_code"] == "provider_http_503"
        assert client.get(f"/api/apps/{app_id}/conversations").json() == []
    else:
        assert len(attempts) == 2 and frames[-1][0] == "done", frames
        result = frames[-1][1]
        assert result["agent"]["model_calls"] == 2
        # The first request failed without reporting its token usage. Do not
        # silently turn an unknown charge into zero in the aggregate.
        assert result["agent"]["usage"] == {"prompt_tokens": None, "completion_tokens": None}
        assert [turn["state"] for turn in result["agent"]["model_turns"]] == ["failed", "completed"]
        assert sum(event["kind"] == "model_retry" for event in result["agent"]["trace"]) == 1


def test_tool_turn_resets_answer_preview_before_final_answer(client, platform, monkeypatch):
    app_id, _ = enable(platform)
    temporary, final = "尚未核对的临时答案 [1]。", "经资料核对的最终答案 [1]。"

    async def planner(profile, messages, definitions):
        if len(messages) == 2:
            provisional = answer_reply(answer=temporary)
            provisional["tool_calls"] = [call("read_skill", {"name": "course-evidence"})]
            return provisional
        return answer_reply(answer=final)

    streamer(monkeypatch, planner)
    frames = events(client.post(f"/api/apps/{app_id}/chat/stream", json={"message": "导数知识"}))
    assert frames[-1][0] == "done", frames
    text, before_reset, resets = "", "", 0
    for kind, value in frames:
        if kind == "delta":
            text += value["text"]
        elif kind == "reset":
            before_reset, text = text, ""
            resets += 1
    assert resets == 1 and before_reset == temporary
    assert text == final == frames[-1][1]["answer"]
    assert frames[-1][1]["agent"]["model_calls"] == 2
    assert temporary not in json.dumps(frames[-1][1], ensure_ascii=False)


def test_legacy_release_pins_one_runtime_skill_snapshot(client, platform, monkeypatch, tmp_path):
    app_id, _ = enable(platform, skills=["course-evidence"])
    with Session(engine()) as db:
        app = db.get(Application, app_id)
        published = dict(app.published)
        published.pop("skill_snapshot")
        app.published = published
        db.commit()
    root = tmp_path / "runtime-skills"
    package = root / "course-evidence"
    references = package / "references"
    references.mkdir(parents=True)
    frontmatter = "---\nname: course-evidence\ndescription: 稳定方法测试\n---\n"
    source = package / "SKILL.md"
    reference = references / "guide.md"
    source.write_text(frontmatter + "本轮开始时的方法正文 A", encoding="utf-8")
    reference.write_text("本轮开始时的参考资料 A", encoding="utf-8")
    monkeypatch.setattr(skills, "ROOT", root)

    async def planner(profile, messages, definitions):
        if len(messages) == 2:
            assert json.loads(messages[1]["content"])["available_skills"][0]["description"] == "稳定方法测试"
            source.write_text(frontmatter + "本轮执行中的新版正文 B", encoding="utf-8")
            reference.write_text("本轮执行中的新版参考 B", encoding="utf-8")
            return reply(call("read_skill", {"name": "course-evidence"}))
        if len(messages) == 4:
            assert json.loads(messages[-1]["content"])["instructions"] == "本轮开始时的方法正文 A"
            return reply(call("read_skill_reference", {"name": "course-evidence", "path": "references/guide.md"},
                              key="reference"))
        assert json.loads(messages[-1]["content"])["content"] == "本轮开始时的参考资料 A"
        return answer_reply()

    streamer(monkeypatch, planner)
    result = post(client, f"/apps/{app_id}/chat", {"message": "导数知识"})
    information = result["agent"]["skill_snapshot"]
    assert information["source"] == "legacy_live" and information["pinned"] is False
    assert information["sha256"] is None and len(information["runtime_sha256"]) == 64
    with Session(engine()) as db:
        assert "skill_snapshot" not in db.get(Application, app_id).published


def test_skill_catalog_and_configuration_are_admin_only_and_publish_scoped(client, platform):
    available = client.get("/api/admin/agent-capabilities").json()
    assert {s["name"] for s in available["skills"]} == set(AgentOptions().skills)
    assert all("instructions" not in s and len(s["sha256"]) == 64 for s in available["skills"])
    app = platform["bundles"][0]["app"]
    draft = {**app["draft"], "agent": {"enabled": True}}
    assert client.put(f"/api/admin/apps/{app['id']}", json=draft).status_code == 200
    with Session(engine()) as db:
        assert not agents.options(db.get(Application, app["id"])).enabled
    task = post(client, f"/admin/apps/{app['id']}/publish")
    assert task["state"] == "succeeded"
    with Session(engine()) as db:
        assert agents.options(db.get(Application, app["id"])).enabled
    draft["agent"]["skills"] = ["unknown-skill"]
    assert client.put(f"/api/admin/apps/{app['id']}", json=draft).status_code == 422
    post(client, "/auth/register", {"username": "student", "password": "student-test-password"})
    assert client.get("/api/admin/agent-capabilities").status_code == 403


def test_skill_reference_cannot_escape_package(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    references = root / "sample" / "references"
    references.mkdir(parents=True)
    (references / "valid.md").write_text("参考资料")
    secret = tmp_path / "secret.md"
    secret.write_text("secret")
    (references / "link.md").symlink_to(secret)
    monkeypatch.setattr(skills, "ROOT", root)
    assert skills.read_reference("sample", "references/valid.md") == "参考资料"
    for path in ("../../secret.md", str(secret), "references/link.md", "scripts/run.py"):
        with pytest.raises(ValueError):
            skills.read_reference("sample", path)
