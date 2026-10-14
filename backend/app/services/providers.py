import hashlib
import json
import math
import re
from contextlib import aclosing
from contextvars import ContextVar
from functools import wraps
from urllib.parse import urlparse

import httpx

from ..config import settings
from ..models import ModelProfile
from ..security import decrypt


# Chat and evaluation activate admission. Publishing/ingestion may hold a write
# transaction and must not acquire another SQLite write connection inside it.
model_limits = ContextVar("chat_model_limits", default=False)


def check_sync_cancellation():
    if model_limits.get():
        from .admission import check_worker_cancelled
        check_worker_cancelled()


def limited_call(function):
    @wraps(function)
    def wrapped(profile, *args, **kwargs):
        if not model_limits.get() or profile.provider == "demo":
            return function(profile, *args, **kwargs)
        from .admission import model_slot_sync
        with model_slot_sync(getattr(profile, "id", None)):
            return function(profile, *args, **kwargs)
    return wrapped


def limited_stream(function):
    @wraps(function)
    async def wrapped(profile, *args, **kwargs):
        from .admission import model_slot
        key = getattr(profile, "id", None) if model_limits.get() else None
        async with model_slot(key):
            async with aclosing(function(profile, *args, **kwargs)) as stream:
                async for item in stream:
                    yield item
    return wrapped


class ProviderError(Exception):
    def __init__(self, message, *, code="provider_error", retryable=False):
        super().__init__(message)
        self.code, self.retryable = code, retryable


def authorization(api_key):
    # Local OpenAI-compatible servers may intentionally require no API key.
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def json_response(text: str):
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except ValueError as exc:
        raise ProviderError("模型未返回有效的结构化数据，请重试", code="invalid_json") from exc


@limited_call
def generate(profile: ModelProfile, system: str, prompt: str | list, structured=False, *, max_tokens: int | None = None, thinking=None, reasoning_effort=None, request_timeout=None):
    """Use provider output defaults unless a processing task explicitly requests a budget."""
    cfg = settings()
    truncated = ("模型服务达到自身输出或上下文上限，回答被截断，请分步提问"
                 if max_tokens is None else "模型输出被截断，请减少单次处理的内容或增加输出上限")
    if profile.provider == "demo":
        if not cfg.allow_demo_provider:
            raise ProviderError("演示模型未启用")
        raise ProviderError("演示模式只展示检索证据，不执行大模型生成")
    if isinstance(prompt, list) and profile.provider != "openai":
        raise ProviderError("图文输入需要配置支持图片输入的 OpenAI 兼容模型")
    try:
        with httpx.Client(timeout=request_timeout or cfg.model_timeout, follow_redirects=False) as client:
            if profile.provider == "ollama":
                payload = {"model": profile.model, "stream": False,
                           "messages": [{"role": "system", "content": system},
                                        {"role": "user", "content": prompt}],
                           "options": {"temperature": profile.temperature,
                                       "num_predict": -1 if max_tokens is None else max_tokens}}
                if structured:
                    payload["format"] = "json"
                check_sync_cancellation()
                response = client.post(profile.base_url.rstrip("/") + "/api/chat", json=payload)
                response.raise_for_status()
                if response.json().get("done_reason") == "length":
                    raise ProviderError(truncated)
                text = response.json()["message"]["content"]
                usage = {"prompt_tokens": response.json().get("prompt_eval_count"),
                         "completion_tokens": response.json().get("eval_count")}
            else:
                payload = {"model": profile.model, "temperature": profile.temperature,
                           "messages": [{"role": "system", "content": system},
                                        {"role": "user", "content": prompt}]}
                # Omit the field entirely: null/negative values are not portable across gateways.
                if max_tokens is not None:
                    payload["max_tokens"] = max_tokens
                if structured:
                    payload["response_format"] = {"type": "json_object"}
                # Official DeepSeek option; do not send provider-specific fields to other gateways.
                if thinking is not None and urlparse(profile.base_url).hostname == "api.deepseek.com":
                    payload["thinking"] = {"type": "enabled" if thinking else "disabled"}
                    if thinking and reasoning_effort:
                        payload["reasoning_effort"] = reasoning_effort
                check_sync_cancellation()
                response = client.post(profile.base_url.rstrip("/") + "/chat/completions", json=payload,
                                       headers=authorization(decrypt(profile.secret)))
                response.raise_for_status()
                finish = response.json()["choices"][0].get("finish_reason")
                if finish == "length":
                    raise ProviderError(truncated)
                if finish not in {None, "stop"}:
                    raise ProviderError("模型没有正常完成回答，请稍后重试")
                text = response.json()["choices"][0]["message"]["content"]
                usage = response.json().get("usage", {})
    except httpx.TimeoutException as exc:
        raise ProviderError("模型请求超时，请稍后重试") from exc
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        # Never expose URLs, provider response bodies, or credentials.
        raise ProviderError("模型服务不可用，请检查模型名称、服务地址及凭据") from exc
    if structured:
        if not isinstance(text, str) or not text.strip():
            raise ProviderError("模型未返回可用的结构化正文，请重试")
        decoded = json_response(text)
        if not isinstance(decoded, dict):
            raise ProviderError("模型未返回 JSON 对象，请重试")
        decoded["_usage"] = usage
        return decoded
    return text


@limited_stream
async def stream_generate(profile: ModelProfile, system: str, prompt: str):
    """Yield actual provider content deltas; never expose reasoning or protocol metadata."""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    payload = {"model": profile.model, "messages": messages, "stream": True}
    headers = {}
    if profile.provider == "ollama":
        url = profile.base_url.rstrip("/") + "/api/chat"
        payload.update(format="json", options={"temperature": profile.temperature, "num_predict": -1})
    elif profile.provider == "openai":
        url = profile.base_url.rstrip("/") + "/chat/completions"
        payload.update(temperature=profile.temperature, response_format={"type": "json_object"})
        headers = authorization(decrypt(profile.secret))
    else:
        raise ProviderError("此模型不支持流式生成")
    finished = False
    try:
        async with httpx.AsyncClient(timeout=settings().model_timeout, follow_redirects=False) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as response:
                response.raise_for_status()
                async for event in _provider_events(response, profile.provider):
                    if event == "[DONE]":
                        if not finished:
                            raise ProviderError("模型连接提前结束，回答未完成，请重试")
                        return
                    item = json.loads(event)
                    if not isinstance(item, dict) or "error" in item:
                        raise ProviderError("模型流式服务返回错误，请重试")
                    if profile.provider == "ollama":
                        content = item.get("message", {}).get("content", "")
                        reason = item.get("done_reason") if item.get("done") else None
                    else:
                        choices = item.get("choices", [])
                        if not choices:
                            continue  # Some compatible providers send a separate usage event.
                        content = choices[0].get("delta", {}).get("content") or ""
                        reason = choices[0].get("finish_reason")
                    if reason == "length":
                        raise ProviderError("模型服务达到自身输出或上下文上限，回答被截断，请分步提问")
                    if reason not in {None, "stop"}:
                        raise ProviderError("模型没有正常完成回答，请稍后重试")
                    if not isinstance(content, str) or (finished and content):
                        raise ProviderError("模型流式响应格式不正确，请重试")
                    if content:
                        yield content
                    if profile.provider == "ollama" and item.get("done"):
                        return
                    if reason == "stop":
                        finished = True
    except httpx.TimeoutException as exc:
        raise ProviderError("模型流式响应超时，回答未完成，请重试") from exc
    except (httpx.HTTPError, KeyError, ValueError, TypeError, AttributeError, IndexError) as exc:
        raise ProviderError("模型流式连接异常，回答未完成，请重试") from exc
    raise ProviderError("模型连接提前结束，回答未完成，请重试")


async def _provider_events(response, provider):
    data = []
    async for line in response.aiter_lines():
        if provider == "ollama":
            if line.strip():
                yield line
        elif not line:
            if data:
                yield "\n".join(data)
                data = []
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
        # SSE comments, event names, and ids do not contain answer text.
    if data:
        yield "\n".join(data)


async def tool_turn(profile, messages, tools):
    """One native function-calling round. Callers execute only their registered tools.

    Keep provider-specific assistant fields (e.g. reasoning_content/thinking) in the
    private conversation so reasoning models can continue their tool-call turn.
    They are never returned in UI events or persisted in chat history.
    """
    payload = {"model": profile.model, "messages": messages, "stream": False, "tools": tools}
    headers = {}
    if profile.provider == "ollama":
        url = profile.base_url.rstrip("/") + "/api/chat"
        payload["options"] = {"temperature": profile.temperature, "num_predict": -1}
    elif profile.provider == "openai":
        url = profile.base_url.rstrip("/") + "/chat/completions"
        payload.update(temperature=profile.temperature, tool_choice="auto")
        headers = authorization(decrypt(profile.secret))
    else:
        raise ProviderError("此模型不支持工具调用")
    try:
        async with httpx.AsyncClient(timeout=settings().model_timeout, follow_redirects=False) as client:
            response = await client.post(url, json=payload, headers=headers)
            if response.status_code in {400, 404, 422}:
                raise ProviderError("模型拒绝工具调用请求，请选择支持 Tool Use 的模型或关闭应用的 Agent 模式")
            response.raise_for_status()
            body = response.json()
        if profile.provider == "ollama":
            message, reason = body["message"], body.get("done_reason")
            if body.get("done") is not True:
                raise ProviderError("模型工具调用响应未完成，请重试")
        else:
            choice = body["choices"][0]
            message, reason = choice["message"], choice.get("finish_reason")
        if reason == "length":
            raise ProviderError("模型服务达到自身输出或上下文上限，工具调用被截断，请分步提问")
        if reason not in {"stop", "tool_calls", None}:
            raise ProviderError("模型没有正常完成工具调用，请重试")
        if not isinstance(message, dict) or message.get("role", "assistant") != "assistant":
            raise ValueError("invalid assistant")
        content = message.get("content") or ""
        calls = message.get("tool_calls") or []
        if not isinstance(content, str) or not isinstance(calls, list) or len(calls) > 8:
            raise ValueError("invalid calls")
        if reason == "tool_calls" and not calls:
            raise ValueError("missing calls")
        ids = set()
        for call in calls:
            function = call["function"]
            if call.get("type", "function") != "function" or not isinstance(function["name"], str):
                raise ValueError("invalid function")
            arguments = function["arguments"]
            if not isinstance(arguments, (str, dict)) or len(json.dumps(arguments)) > 16000:
                raise ValueError("invalid arguments")
            if profile.provider == "openai":
                call_id = call["id"]
                if not isinstance(call_id, str) or not call_id or call_id in ids:
                    raise ValueError("invalid call id")
                ids.add(call_id)
        # Whitelist protocol fields; do not echo arbitrary provider response metadata.
        return {"role": "assistant", "content": content,
                **({"tool_calls": calls} if calls else {}),
                **{k: message[k] for k in ("reasoning_content", "thinking") if isinstance(message.get(k), str)}}
    except httpx.TimeoutException as exc:
        raise ProviderError("模型工具调用超时，请稍后重试") from exc
    except (httpx.HTTPError, KeyError, ValueError, TypeError, AttributeError, IndexError) as exc:
        raise ProviderError("模型工具调用服务异常，请检查模型与 Tool Use 支持") from exc


def tool_result_message(profile, call, result):
    message = {"role": "tool", "content": json.dumps(result, ensure_ascii=False)}
    if profile.provider == "ollama":
        message["tool_name"] = call["function"]["name"]
    else:
        message["tool_call_id"] = call["id"]
    return message


def _token_usage(value):
    value = value if isinstance(value, dict) else {}
    return {key: value.get(key) if type(value.get(key)) is int and value[key] >= 0 else None
            for key in ("prompt_tokens", "completion_tokens")}


@limited_stream
async def stream_tool_turn(profile, messages, tools):
    """Stream one answer-or-tools turn, preserving private protocol fields for the next round.

    Deltas are provisional until `done`; no tool executes before complete validation.
    Unlike the legacy preparation call, the same request may directly answer a question.
    """
    payload = {"model": profile.model, "messages": messages, "stream": True}
    headers = {}
    if tools:
        payload["tools"] = tools
    if profile.provider == "ollama":
        url = profile.base_url.rstrip("/") + "/api/chat"
        payload["options"] = {"temperature": profile.temperature, "num_predict": -1}
        if not tools:
            payload["format"] = "json"
    elif profile.provider == "openai":
        url = profile.base_url.rstrip("/") + "/chat/completions"
        payload.update(temperature=profile.temperature, stream_options={"include_usage": True})
        if tools:
            payload["tool_choice"] = "auto"
        else:
            payload["response_format"] = {"type": "json_object"}
        headers = authorization(decrypt(profile.secret))
    else:
        raise ProviderError("此模型不支持工具调用", code="unsupported_provider")
    content, reasoning, thinking, calls = [], [], [], {}
    finished, finish_reason, usage = False, None, _token_usage(None)
    try:
        async with httpx.AsyncClient(timeout=settings().model_timeout, follow_redirects=False) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as response:
                if response.status_code in {400, 404, 422}:
                    raise ProviderError("模型拒绝流式工具请求，请检查 Tool Use 与流式协议支持",
                                        code="unsupported_tool_protocol")
                response.raise_for_status()
                async for event in _provider_events(response, profile.provider):
                    if event == "[DONE]":
                        break
                    item = json.loads(event)
                    if not isinstance(item, dict) or "error" in item:
                        raise ValueError("invalid stream event")
                    if profile.provider == "ollama":
                        if "done" in item and type(item["done"]) is not bool:
                            raise ValueError("invalid completion flag")
                        delta = item.get("message", {})
                        reason = item.get("done_reason", "stop") if item.get("done") else None
                        if item.get("done"):
                            usage = _token_usage({"prompt_tokens": item.get("prompt_eval_count"),
                                                  "completion_tokens": item.get("eval_count")})
                    else:
                        if item.get("usage") is not None:
                            usage = _token_usage(item["usage"])
                        choices = item.get("choices", [])
                        if not choices:
                            continue
                        if len(choices) != 1:
                            raise ValueError("multiple choices")
                        delta, reason = choices[0].get("delta", {}), choices[0].get("finish_reason")
                    if not isinstance(delta, dict) or delta.get("role", "assistant") != "assistant":
                        raise ValueError("invalid delta")
                    if finished and any(delta.get(k) for k in ("content", "tool_calls", "thinking", "reasoning_content")):
                        raise ValueError("content after finish")
                    if reason == "length":
                        raise ProviderError("模型服务达到自身输出或上下文上限，回答被截断，请分步提问",
                                            code="output_truncated")
                    if reason not in {None, "stop", "tool_calls"}:
                        raise ProviderError("模型没有正常完成回答，请重试", code="incomplete_response")
                    for field, target in (("content", content), ("thinking", thinking), ("reasoning_content", reasoning)):
                        text = delta.get(field) or ""
                        if not isinstance(text, str):
                            raise ValueError("invalid text delta")
                        if text:
                            target.append(text)
                            if field == "content":
                                yield "delta", text
                    chunks = delta.get("tool_calls") or []
                    if not isinstance(chunks, list):
                        raise ValueError("invalid tool calls")
                    for chunk in chunks:
                        function = chunk.get("function") or {}
                        if profile.provider == "ollama":
                            index = len(calls)
                            calls[index] = {"type": "function", "function": {
                                "name": function["name"], "arguments": function["arguments"]}}
                        else:
                            index = chunk["index"]
                            if type(index) is not int or not 0 <= index < 8:
                                raise ValueError("invalid tool index")
                            call = calls.setdefault(index, {"id": "", "type": "function",
                                                           "function": {"name": "", "arguments": ""}})
                            if chunk.get("type", "function") not in {None, "function"}:
                                raise ValueError("invalid tool type")
                            for key, target in (("id", call), ("name", call["function"])):
                                value = (chunk if key == "id" else function).get(key)
                                if value:
                                    if not isinstance(value, str) or (target[key] and target[key] != value):
                                        raise ValueError("inconsistent tool identity")
                                    target[key] = value
                            arguments = function.get("arguments") or ""
                            if not isinstance(arguments, str):
                                raise ValueError("invalid argument delta")
                            call["function"]["arguments"] += arguments
                        if len(calls) > 8 or len(json.dumps(calls[index]["function"]["arguments"])) > 16000:
                            raise ValueError("tool request too large")
                    if reason is not None:
                        finished, finish_reason = True, reason
                    if profile.provider == "ollama" and item.get("done"):
                        break
        if not finished or (finish_reason == "tool_calls" and not calls):
            raise ProviderError("模型连接提前结束，回答未完成，请重试", code="incomplete_response")
        ordered = [calls[i] for i in sorted(calls)]
        ids = set()
        for call in ordered:
            fn = call["function"]
            if not isinstance(fn["name"], str) or not fn["name"] or not isinstance(fn["arguments"], (dict, str)):
                raise ValueError("invalid completed call")
            if profile.provider == "openai":
                if not call["id"] or call["id"] in ids:
                    raise ValueError("missing or duplicate call id")
                ids.add(call["id"])
        message = {"role": "assistant", "content": "".join(content)}
        if ordered:
            message["tool_calls"] = ordered
        if reasoning:
            message["reasoning_content"] = "".join(reasoning)
        if thinking:
            message["thinking"] = "".join(thinking)
        yield "done", {"message": message, "usage": usage}
    except httpx.TimeoutException as exc:
        raise ProviderError("模型请求超时，请稍后重试", code="provider_timeout", retryable=True) from exc
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        raise ProviderError("模型服务暂时不可用" if code in {429, 500, 502, 503, 504} else "模型服务拒绝请求，请检查配置",
                            code=f"provider_http_{code}", retryable=code in {429, 500, 502, 503, 504}) from exc
    except httpx.TransportError as exc:
        raise ProviderError("模型连接失败，请稍后重试", code="provider_connection", retryable=True) from exc
    except (httpx.HTTPError, KeyError, ValueError, TypeError, AttributeError, IndexError) as exc:
        raise ProviderError("模型流式工具响应格式不正确，请重试", code="invalid_provider_response") from exc


def check_profile(profile):
    if profile.purpose == "embedding":
        result = embed(["微积分模型连接测试"], profile)
        return {"ok": True, "dimension": len(result[0]), "mode": profile.provider, "message": "向量生成调用成功"}
    if profile.provider == "demo" and settings().allow_demo_provider:
        return {"ok": True, "mode": "demo", "message": "演示证据模式，不调用真实模型"}
    generate(profile, "Return a short answer.", "Reply OK.")
    return {"ok": True, "mode": profile.provider, "message": "模型生成调用成功"}


def tokens(text):
    normalized = text.lower()
    chinese = re.findall(r"[\u4e00-\u9fff]+", normalized)
    grams = [s[i:i+2] for s in chinese for i in range(max(1, len(s)-1))]
    return grams + re.findall(r"[a-z][a-z0-9_]*|\d+", normalized)


def embed(texts: list[str], profile=None) -> list[list[float]]:
    if model_limits.get() and profile is not None and profile.provider != "demo":
        from .admission import model_slot_sync
        with model_slot_sync(getattr(profile, "id", None)):
            return _embed(texts, profile)
    return _embed(texts, profile)


def _embed(texts: list[str], profile=None) -> list[list[float]]:
    from types import SimpleNamespace
    cfg = settings()
    if profile:
        cfg = SimpleNamespace(embedding_provider="hash" if profile.provider == "demo" else profile.provider,
            embedding_base_url=profile.base_url, embedding_model=profile.model,
            embedding_dimension=profile.dimension, embedding_api_key=decrypt(profile.secret),
            model_timeout=cfg.model_timeout, allow_demo_provider=cfg.allow_demo_provider)
    if cfg.embedding_provider == "hash":
        if not cfg.allow_demo_provider:
            raise ProviderError("哈希向量仅可用于明确启用的演示模式")
        result = []
        for text in texts:
            vector = [0.0] * cfg.embedding_dimension
            for term in tokens(text):
                vector[int(hashlib.sha256(term.encode()).hexdigest()[:8], 16) % len(vector)] += 1
            norm = math.sqrt(sum(v*v for v in vector)) or 1
            result.append([v/norm for v in vector])
        return result
    try:
        with httpx.Client(timeout=cfg.model_timeout) as client:
            if cfg.embedding_provider == "ollama":
                check_sync_cancellation()
                response = client.post(cfg.embedding_base_url.rstrip("/") + "/api/embed",
                                       json={"model": cfg.embedding_model, "input": texts})
                response.raise_for_status()
                result = response.json()["embeddings"]
            elif cfg.embedding_provider == "openai":
                check_sync_cancellation()
                response = client.post(cfg.embedding_base_url.rstrip("/") + "/embeddings",
                                       headers=authorization(cfg.embedding_api_key),
                                       json={"model": cfg.embedding_model, "input": texts})
                response.raise_for_status()
                result = [item["embedding"] for item in sorted(response.json()["data"], key=lambda x: x["index"])]
            else:
                raise ProviderError("未知 Embedding 提供方")
        if len(result) != len(texts) or any(len(v) != cfg.embedding_dimension for v in result):
            raise ProviderError("Embedding 维度与配置不符，请修改配置并重新发布知识库")
        if any(not all(math.isfinite(x) for x in v) or not any(v) for v in result):
            raise ProviderError("Embedding 返回无效向量")
        return result
    except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
        raise ProviderError("Embedding 服务不可用") from exc


def embedding_signature(profile):
    return hashlib.sha256(json.dumps([profile.provider, profile.base_url, profile.model, profile.dimension]).encode()).hexdigest()


def discover_local(base_url):
    """Only the administrator can call this, for an explicitly configured Ollama endpoint."""
    try:
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            response = client.get(base_url.rstrip("/") + "/api/tags")
            response.raise_for_status()
            result = []
            for model in response.json().get("models", [])[:100]:
                detail = client.post(base_url.rstrip("/") + "/api/show", json={"model": model["name"]})
                detail.raise_for_status()
                data = detail.json()
                capabilities = data.get("capabilities", [])
                dimension = next((v for k, v in data.get("model_info", {}).items() if k.endswith(".embedding_length")), None)
                result.append({"name": model["name"], "capabilities": capabilities, "dimension": dimension,
                               "embedding": "embedding" in capabilities})
            return result
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        raise ProviderError("无法扫描此 Ollama 服务，请检查地址和网络") from exc
