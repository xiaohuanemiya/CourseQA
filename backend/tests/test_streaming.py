import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import engine
from app.models import Knowledge, ModelProfile, User
from app.schemas import ChatInput
from app.services import providers, streaming
from .conftest import post


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.mark.parametrize("size", [1, 2, 7, 10000])
@pytest.mark.parametrize("escaped", [True, False])
def test_answer_decoder_handles_split_unicode_latex_and_nested_keys(size, escaped):
    answer = '中文😀 "引号"\n公式 \\(\\frac{x^2}{2}+C\\) [1]。'
    document = json.dumps({"nested": {"answer": "不要展示"}, "status": "answered",
                           "answer": answer, "citation_ids": ["1"]}, ensure_ascii=escaped)
    decoder = streaming.AnswerDecoder()
    chunks = [decoder.feed(document[i:i+size]) for i in range(0, len(document), size)]
    assert "".join(chunks) == answer
    if size < 10:
        assert sum(bool(c) for c in chunks) > 1


class BytesStream(httpx.AsyncByteStream):
    def __init__(self, packets):
        self.packets, self.closed = packets, False

    async def __aiter__(self):
        for packet in self.packets:
            if isinstance(packet, Exception):
                raise packet
            yield packet

    async def aclose(self):
        self.closed = True


def wire(provider, content=None, reason=None, done=False):
    if provider == "ollama":
        return (json.dumps({"message": {"content": content or ""}, "done": done,
                            "done_reason": reason}, ensure_ascii=False) + "\n").encode()
    return ("data: " + json.dumps({"choices": [{"delta": {"content": content},
             "finish_reason": reason}]}, ensure_ascii=False) + "\r\n\r\n").encode()


@pytest.mark.parametrize("provider", ["openai", "ollama"])
def test_provider_yields_before_completion_and_cancellation_closes_transport(monkeypatch, provider):
    captured = []
    raw = wire(provider, '中文\\n正文')
    # Split multibyte UTF-8 as well as SSE frame boundaries across network packets.
    body = BytesStream([raw[i:i+1] for i in range(len(raw))] + [wire(provider, reason="stop", done=True), b"data: [DONE]\n\n"])
    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, stream=body)
    original = httpx.AsyncClient
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    profile = SimpleNamespace(provider=provider, model="test", base_url="http://local", secret="", temperature=.1)
    async def check():
        generator = providers.stream_generate(profile, "system", "question")
        assert await anext(generator) == '中文\\n正文'
        assert not body.closed
        await generator.aclose()
        assert body.closed
    run(check())
    assert captured[0]["stream"] is True
    assert "max_tokens" not in captured[0]
    if provider == "ollama":
        assert captured[0]["options"]["num_predict"] == -1


@pytest.mark.parametrize("provider", ["openai", "ollama"])
@pytest.mark.parametrize("ending", ["stop", "length", "eof", "timeout", "malformed", "error"])
def test_provider_requires_successful_terminal_event(monkeypatch, provider, ending):
    packets = [wire(provider, "正文")]
    if ending in {"stop", "length"}:
        packets.append(wire(provider, reason=ending, done=True))
        if provider == "openai":
            packets.extend([b'data: {"choices":[],"usage":{}}\n\n', b"data: [DONE]\n\n"])
    elif ending == "timeout":
        packets.append(httpx.ReadTimeout("secret-url-must-not-leak"))
    elif ending == "malformed":
        packets.append(b"broken-json\n" if provider == "ollama" else b"data: broken-json\n\n")
    elif ending == "error":
        packets.append(b'{"error":"secret-error"}\n' if provider == "ollama" else b'data: {"error":"secret-error"}\n\n')
    body = BytesStream(packets)
    original = httpx.AsyncClient
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **kw: original(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=body)), **kw))
    profile = SimpleNamespace(provider=provider, model="test", base_url="http://local", secret="", temperature=.1)
    async def collect():
        return [chunk async for chunk in providers.stream_generate(profile, "system", "question")]
    if ending == "stop":
        assert run(collect()) == ["正文"]
    else:
        with pytest.raises(providers.ProviderError) as exc:
            run(collect())
        assert "secret" not in str(exc.value)
    assert body.closed


def cloud_profile(platform):
    with Session(engine()) as db:
        db.get(ModelProfile, platform["generation"]["id"]).provider = "openai"
        user_id = db.scalar(select(User.id).where(User.username == "admin"))
        db.commit()
    return user_id


def events(response):
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    return [(frame.splitlines()[0][7:], json.loads(frame.splitlines()[1][6:]))
            for frame in response.text.strip().split("\n\n") if frame.startswith("event:")]


@pytest.mark.parametrize("failure", [None, "citation", "invalid_json", "provider", "withdrawn", "cancelled"])
def test_chat_stream_saves_only_complete_valid_current_answer(client, platform, monkeypatch, failure):
    user_id = cloud_profile(platform)
    bundle = platform["bundles"][0]
    app_id = bundle["app"]["id"]
    answer = '导数示例 \\(2x\\) 😀 [1]。' if failure != "citation" else "无效引用 [999]"
    text = json.dumps({"status": "answered", "answer": answer, "citation_ids": ["1"]})
    closed = []
    async def upstream(*args):
        try:
            for i in range(0, len(text), 5):
                yield text[i:i+5]
            if failure == "provider":
                raise providers.ProviderError("模型流式响应超时")
            if failure == "invalid_json":
                yield "broken"
            if failure == "withdrawn":
                with Session(engine()) as db:
                    db.get(Knowledge, bundle["node"]["id"]).withdrawn = True
                    db.commit()
        finally:
            closed.append(True)
    monkeypatch.setattr(streaming, "stream_generate", upstream)
    if failure == "cancelled":
        async def cancel():
            stream = streaming.event_stream(streaming.chat_events(app_id, user_id, ChatInput(message="导数知识")))
            async for event in stream:
                if "event: delta" in event:
                    await stream.aclose()
                    break
        run(cancel())
    else:
        result = events(client.post(f"/api/apps/{app_id}/chat/stream", json={"message": "导数知识"}))
        if failure:
            assert result[-1][0] == "error"
            assert not any(e == "done" for e, _ in result)
        else:
            assert result[-1][0] == "done"
            assert "".join(d["text"] for e, d in result if e == "delta") == answer
            assert sum(e == "delta" for e, _ in result) > 1
            final = result[-1][1]
            history = client.get(f"/api/apps/{app_id}/conversations/{final['conversation_id']}").json()
            assert history[0]["result"] == final
            assert {c["knowledge_id"] for c in final["citations"]} == {bundle["node"]["id"]}
    assert closed
    history = client.get(f"/api/apps/{app_id}/conversations").json()
    assert len(history) == (0 if failure else 1)


def test_stream_scope_empty_retrieval_and_demonstration(client, platform, monkeypatch):
    a, b = platform["bundles"]
    result = events(client.post(f"/api/apps/{a['app']['id']}/chat/stream", json={"message": "导数知识"}))
    final = result[-1][1]
    assert final["mode"] == "demo" and final["status"] == "evidence_only"
    assert {c["knowledge_id"] for c in final["citations"]} == {a["node"]["id"]}
    assert client.post(f"/api/apps/{b['app']['id']}/chat/stream", json={
        "message": "积分知识", "conversation_id": final["conversation_id"]}).status_code == 404
    monkeypatch.setattr(streaming.learning, "retrieve", lambda *a, **kw: [])
    empty = events(client.post(f"/api/apps/{a['app']['id']}/chat/stream", json={"message": "其他知识"}))[-1][1]
    assert empty["status"] == "insufficient_evidence" and empty["citations"] == []
    post(client, "/auth/register", {"username": "other-user", "password": "student-test-password"})
    assert client.post(f"/api/apps/{a['app']['id']}/chat/stream", json={
        "message": "导数知识", "conversation_id": final["conversation_id"]}).status_code == 404
    post(client, "/auth/logout")
    assert client.post(f"/api/apps/{a['app']['id']}/chat/stream", json={"message": "导数知识"}).status_code == 401


def test_heartbeat_and_cancellation_close_pending_generator():
    closed = []
    async def blocked():
        try:
            await asyncio.Event().wait()
            yield "done", {}
        finally:
            closed.append(True)
    async def check():
        stream = streaming.event_stream(blocked(), heartbeat_seconds=.01)
        assert await anext(stream) == ": keep-alive\n\n"
        await stream.aclose()
    run(check())
    assert closed
