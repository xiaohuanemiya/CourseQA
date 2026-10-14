"""Wire-level checks for native streamed answer/tool turns, independent of agents."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app.services import providers
from .test_streaming import BytesStream


TOOLS = [{"type": "function", "function": {"name": "search_knowledge", "description": "Search",
          "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}}}]


def profile(provider="openai"):
    return SimpleNamespace(provider=provider, model="test", base_url="http://local", secret="", temperature=.1)


def frame(provider="openai", delta=None, reason=None, **extra):
    if provider == "ollama":
        value = {"message": delta or {}, "done": reason is not None, **extra}
        if reason is not None:
            value["done_reason"] = reason
        return (json.dumps(value, ensure_ascii=False) + "\n").encode()
    value = {"choices": [{"delta": delta or {}, "finish_reason": reason}], **extra}
    return ("data: " + json.dumps(value, ensure_ascii=False) + "\r\n\r\n").encode()


def transport(monkeypatch, bodies, status=200):
    captured = []
    queue = iter(bodies)

    def handler(request):
        assert "authorization" not in request.headers
        captured.append(json.loads(request.content))
        return httpx.Response(status, stream=next(queue))

    original = httpx.AsyncClient
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs))
    return captured


async def collect(provider="openai", messages=None, tools=TOOLS):
    return [event async for event in providers.stream_tool_turn(profile(provider), messages or [], tools)]


def chunk(index, name=None, arguments=None, call_id=None):
    function = {}
    if name is not None:
        function["name"] = name
    if arguments is not None:
        function["arguments"] = arguments
    return {"index": index, "function": function, **({"id": call_id, "type": "function"} if call_id else {})}


def test_openai_interleaved_arguments_usage_and_private_reasoning_survive_next_turn(monkeypatch):
    first = b"".join([
        b": keep alive\r\nevent: message\r\nid: private-event-id\r\n",
        frame(delta={"role": "assistant", "reasoning_content": "private reasoning ", "thinking": "private thought ",
                     "tool_calls": [chunk(1, "search_knowledge", '{"query":"积', "second"),
                                    chunk(0, "search_knowledge", '{"query":"导', "first")]}),
        frame(delta={"reasoning_content": "continued", "thinking": "continued",
                     "tool_calls": [chunk(0, arguments='数"}'), chunk(1, arguments='分"}')]}),
        frame(reason="tool_calls"),
        b'data: {"choices":[],"usage":{"prompt_tokens":19,"completion_tokens":8}}\r\n\r\n',
        b"data: [DONE]\r\n\r\n",
    ])
    # Split UTF-8 characters, event framing, and argument fragments at the byte level.
    body = BytesStream([first[i:i + 1] for i in range(len(first))])
    answer = BytesStream([frame(delta={"content": "已找到课程资料"}), frame(reason="stop"), b"data: [DONE]\n\n"])
    captured = transport(monkeypatch, [body, answer])

    async def check():
        events = await collect()
        assert [kind for kind, _ in events] == ["done"]
        finished = events[-1][1]
        assert finished["usage"] == {"prompt_tokens": 19, "completion_tokens": 8}
        message = finished["message"]
        assert message["reasoning_content"] == "private reasoning continued"
        assert message["thinking"] == "private thought continued"
        calls = message["tool_calls"]
        assert [call["id"] for call in calls] == ["first", "second"]
        assert [json.loads(call["function"]["arguments"]) for call in calls] == [{"query": "导数"}, {"query": "积分"}]
        messages = [{"role": "user", "content": "课程问题"}, message]
        messages += [providers.tool_result_message(profile(), call, {"evidence": []}) for call in calls]
        answer_events = await collect(messages=messages)
        assert [value for kind, value in answer_events if kind == "delta"] == ["已找到课程资料"]

    asyncio.run(check())
    assert captured[0]["stream"] is True and captured[0]["stream_options"] == {"include_usage": True}
    assert captured[0]["tool_choice"] == "auto" and "response_format" not in captured[0]
    assert "max_tokens" not in captured[0]
    assert captured[1]["messages"][1]["reasoning_content"] == "private reasoning continued"
    assert [value["tool_call_id"] for value in captured[1]["messages"][2:]] == ["first", "second"]
    assert body.closed and answer.closed


def test_ollama_multiple_tools_in_ndjson_preserve_thinking_and_tool_name_protocol(monkeypatch):
    first = b"".join([
        frame("ollama", {"thinking": "private thought", "tool_calls": [
            {"function": {"name": "search_knowledge", "arguments": {"query": "导数"}}}]}),
        frame("ollama", {"tool_calls": [
            {"function": {"name": "calculate", "arguments": {"expression": "x^2", "operation": "differentiate"}}}]}),
        frame("ollama", reason="stop", prompt_eval_count=23, eval_count=11),
    ])
    body = BytesStream([first[i:i + 3] for i in range(0, len(first), 3)])
    answer = BytesStream([frame("ollama", {"content": "答案"}, reason="stop")])
    captured = transport(monkeypatch, [body, answer])

    async def check():
        events = await collect("ollama")
        assert [kind for kind, _ in events] == ["done"]
        final = events[-1][1]
        assert final["usage"] == {"prompt_tokens": 23, "completion_tokens": 11}
        message = final["message"]
        assert message["thinking"] == "private thought"
        assert len(message["tool_calls"]) == 2
        messages = [message] + [providers.tool_result_message(profile("ollama"), call, {"result": "ok"})
                               for call in message["tool_calls"]]
        await collect("ollama", messages=messages)

    asyncio.run(check())
    assert captured[0]["options"]["num_predict"] == -1
    assert "format" not in captured[0]
    assert captured[1]["messages"][0]["thinking"] == "private thought"
    assert [value["tool_name"] for value in captured[1]["messages"][1:]] == ["search_knowledge", "calculate"]
    assert all("tool_call_id" not in value for value in captured[1]["messages"][1:])
    assert body.closed and answer.closed


@pytest.mark.parametrize("provider", ["openai", "ollama"])
def test_answer_turn_exposes_only_content_deltas_and_no_tool_budget_requests_json(monkeypatch, provider):
    packets = [frame(provider, {"thinking": "secret thought", "reasoning_content": "secret reasoning"}),
               frame(provider, {"content": "中文"}), frame(provider, {"content": "答案"}, reason="stop")]
    if provider == "openai":
        packets.append(b"data: [DONE]\n\n")
    body = BytesStream(packets)
    captured = transport(monkeypatch, [body])
    events = asyncio.run(collect(provider, tools=[]))
    assert [value for kind, value in events if kind == "delta"] == ["中文", "答案"]
    assert events[-1][1]["message"]["content"] == "中文答案"
    assert events[-1][1]["message"]["thinking"] == "secret thought"
    assert events[-1][1]["usage"] == {"prompt_tokens": None, "completion_tokens": None}
    assert "tools" not in captured[0]
    if provider == "openai":
        assert captured[0]["response_format"] == {"type": "json_object"}
    else:
        assert captured[0]["format"] == "json"
    assert body.closed


@pytest.mark.parametrize("provider", ["openai", "ollama"])
@pytest.mark.parametrize("failure,code,retryable", [
    ("eof", "incomplete_response", False), ("length", "output_truncated", False),
    ("timeout", "provider_timeout", True), ("connection", "provider_connection", True),
    ("malformed", "invalid_provider_response", False), ("error", "invalid_provider_response", False),
])
def test_failed_streams_do_not_emit_completed_turn(monkeypatch, provider, failure, code, retryable):
    packets = [frame(provider, {"content": "未完成"})]
    if failure == "length":
        packets.append(frame(provider, reason="length"))
    elif failure == "timeout":
        packets.append(httpx.ReadTimeout("secret credentials"))
    elif failure == "connection":
        packets.append(httpx.ConnectError("secret credentials"))
    elif failure == "malformed":
        packets.append(b"secret-invalid-json\n" if provider == "ollama" else b"data: secret-invalid-json\n\n")
    elif failure == "error":
        raw = b'{"error":"secret credentials"}'
        packets.append(raw + b"\n" if provider == "ollama" else b"data: " + raw + b"\n\n")
    body = BytesStream(packets)
    transport(monkeypatch, [body])
    received = []

    async def check():
        async for event in providers.stream_tool_turn(profile(provider), [], TOOLS):
            received.append(event)

    with pytest.raises(providers.ProviderError) as error:
        asyncio.run(check())
    assert error.value.code == code and error.value.retryable is retryable
    assert "secret" not in str(error.value)
    assert received == [("delta", "未完成")]
    assert body.closed


@pytest.mark.parametrize("ending", ["early_done", "missing_calls", "duplicate_id", "identity_changes",
                                   "missing_name", "bad_index", "too_many", "argument_too_large", "after_finish"])
def test_openai_malformed_tool_calls_never_become_executable(monkeypatch, ending):
    if ending == "early_done":
        packets = [b"data: [DONE]\n\n"]
    elif ending == "missing_calls":
        packets = [frame(reason="tool_calls")]
    elif ending == "duplicate_id":
        packets = [frame(delta={"tool_calls": [chunk(0, "search_knowledge", "{}", "same"),
                                                chunk(1, "calculate", "{}", "same")]}, reason="tool_calls")]
    elif ending == "identity_changes":
        packets = [frame(delta={"tool_calls": [chunk(0, "search_knowledge", "{}", "a")]}),
                   frame(delta={"tool_calls": [chunk(0, "calculate", "{}", "a")]}, reason="tool_calls")]
    elif ending == "missing_name":
        packets = [frame(delta={"tool_calls": [chunk(0, arguments="{}", call_id="a")]}, reason="tool_calls")]
    elif ending == "bad_index":
        packets = [frame(delta={"tool_calls": [chunk(True, "calculate", "{}", "a")]}, reason="tool_calls")]
    elif ending == "too_many":
        packets = [frame(delta={"tool_calls": [chunk(i, "calculate", "{}", str(i)) for i in range(9)]})]
    elif ending == "argument_too_large":
        packets = [frame(delta={"tool_calls": [chunk(0, "search_knowledge", "a" * 16001, "a")]})]
    else:
        packets = [frame(reason="stop"), frame(delta={"content": "late private text"})]
    body = BytesStream(packets)
    transport(monkeypatch, [body])
    with pytest.raises(providers.ProviderError) as error:
        asyncio.run(collect())
    expected = "incomplete_response" if ending in {"early_done", "missing_calls"} else "invalid_provider_response"
    assert error.value.code == expected and error.value.retryable is False
    assert body.closed


@pytest.mark.parametrize("status,code,retryable", [
    (429, "provider_http_429", True), (503, "provider_http_503", True),
    (400, "unsupported_tool_protocol", False), (404, "unsupported_tool_protocol", False),
    (422, "unsupported_tool_protocol", False), (401, "provider_http_401", False),
])
def test_http_errors_have_safe_retry_classification(monkeypatch, status, code, retryable):
    body = BytesStream([b'{"error":"secret credentials or service url"}'])
    captured = transport(monkeypatch, [body], status=status)
    with pytest.raises(providers.ProviderError) as error:
        asyncio.run(collect())
    assert error.value.code == code and error.value.retryable is retryable
    assert "secret" not in str(error.value)
    assert len(captured) == 1  # Provider classifies; orchestration owns retry policy.
    assert body.closed


@pytest.mark.parametrize("provider", ["openai", "ollama"])
def test_cancelling_after_first_delta_closes_upstream(monkeypatch, provider):
    body = BytesStream([frame(provider, {"content": "第一段"}), frame(provider, reason="stop")])
    transport(monkeypatch, [body])

    async def check():
        generator = providers.stream_tool_turn(profile(provider), [], TOOLS)
        assert await anext(generator) == ("delta", "第一段")
        assert not body.closed
        await generator.aclose()
        assert body.closed

    asyncio.run(check())


def test_invalid_usage_numbers_are_not_counted(monkeypatch):
    body = BytesStream([frame(delta={"content": "答案"}, reason="stop", usage={
        "prompt_tokens": True, "completion_tokens": -5}), b"data: [DONE]\n\n"])
    transport(monkeypatch, [body])
    result = asyncio.run(collect())[-1][1]
    assert result["usage"] == {"prompt_tokens": None, "completion_tokens": None}


@pytest.mark.parametrize("done", ["false", "true", 1])
def test_ollama_requires_boolean_completion_marker(monkeypatch, done):
    packet = json.dumps({"message": {"content": "unfinished"}, "done": done}).encode() + b"\n"
    body = BytesStream([packet])
    transport(monkeypatch, [body])
    with pytest.raises(providers.ProviderError) as error:
        asyncio.run(collect("ollama"))
    assert error.value.code == "invalid_provider_response"
    assert body.closed
