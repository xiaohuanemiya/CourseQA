"""Incremental answer decoding and authenticated, cancellable chat event streams."""
import asyncio
import json
import logging
from contextlib import aclosing, suppress

import anyio
from fastapi import HTTPException
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..db import engine
from . import learning, providers
from .async_db import run_db
from .providers import ProviderError, json_response, stream_generate

logger = logging.getLogger(__name__)


class AnswerDecoder:
    """Read only the top-level answer string, including escapes split across deltas.

    The complete JSON is still parsed and validated before anything is persisted.
    This scanner processes every character once rather than reparsing growing JSON.
    """
    def __init__(self):
        self.depth = 0
        self.in_string = False
        self.role = None
        self.key = None
        self.expect_key = False
        self.expect_value = False
        self.key_chars = []
        self.escape = ""
        self.high_surrogate = None
        self.seen_answer = False

    def feed(self, text):
        output = []
        for char in text:
            if not self.in_string:
                if char == '"':
                    self.in_string = True
                    self.role = "key" if self.depth == 1 and self.expect_key else (
                        "answer" if self.depth == 1 and self.expect_value and self.key == "answer" else None)
                    self.key_chars = []
                    if self.role == "answer":
                        if self.seen_answer:
                            raise ProviderError("模型回答包含重复正文，请重试")
                        self.seen_answer = True
                elif char in "{[":
                    self.depth += 1
                    if self.depth == 1:
                        self.expect_key = True
                elif char in "}]":
                    self.depth -= 1
                elif char == ":" and self.depth == 1:
                    self.expect_value = True
                elif char == "," and self.depth == 1:
                    self.expect_key, self.expect_value, self.key = True, False, None
                continue
            if self.escape:
                self.escape += char
                if self.escape == "\\u" or (self.escape.startswith("\\u") and len(self.escape) < 6):
                    continue
                try:
                    decoded = json.loads('"' + self.escape + '"')
                except ValueError as exc:
                    raise ProviderError("模型正文的转义格式不正确，请重试") from exc
                self.escape = ""
            elif char == "\\":
                self.escape = char
                continue
            elif char == '"':
                if self.high_surrogate is not None:
                    raise ProviderError("模型正文字符不完整，请重试")
                self.in_string = False
                if self.role == "key":
                    self.key = "".join(self.key_chars)
                    self.expect_key = False
                else:
                    self.expect_value = False
                continue
            else:
                decoded = char
            code = ord(decoded)
            if self.high_surrogate is not None:
                if not 0xDC00 <= code <= 0xDFFF:
                    raise ProviderError("模型正文字符不完整，请重试")
                decoded = chr(0x10000 + (self.high_surrogate - 0xD800) * 0x400 + code - 0xDC00)
                self.high_surrogate = None
            elif 0xD800 <= code <= 0xDBFF:
                self.high_surrogate = code
                continue
            elif 0xDC00 <= code <= 0xDFFF:
                raise ProviderError("模型正文字符不完整，请重试")
            if self.role == "answer":
                output.append(decoded)
            elif self.role == "key":
                self.key_chars.append(decoded)
        return "".join(output)


async def _answer_events(app_id, user_id, data, *, execution=None, stream=True, use_graph=True):
    # Own the session for the stream lifetime, independent of FastAPI dependency cleanup.
    with Session(engine(), expire_on_commit=False) as db:
        yield "status", {"message": "正在检索资料…"}
        from .agents import AgentRuntime, record_failure, should_run
        runtime = None
        saved = False
        try:
            def prepare():
                prepared = learning.prepare_chat(db, app_id, user_id, data, use_graph,
                                                 release_read_connection=True)
                # Return the read connection before waiting for external models or their queue.
                db.commit()
                return prepared

            prepared = await run_db(prepare)
            app, versions, conversation, evidence, profile, result = prepared
            if should_run(app, profile, evidence):
                runtime = AgentRuntime(db, app, versions, evidence, profile, data, result)
                async with aclosing(runtime.events()) as agent_events:
                    async for event in agent_events:
                        yield event
            elif evidence and profile.provider != "demo":
                yield "status", {"message": "正在生成回答…"}
                system, prompt = learning.generation_prompt(app, data, evidence)
                if stream:
                    decoder, chunks, preview = AnswerDecoder(), [], []
                    async with aclosing(stream_generate(profile, system, prompt)) as upstream:
                        async for chunk in upstream:
                            chunks.append(chunk)
                            delta = decoder.feed(chunk)
                            if delta:
                                preview.append(delta)
                                yield "delta", {"text": delta}
                    response = json_response("".join(chunks))
                    learning.apply_answer(result, response, evidence)
                    if result["answer"] != "".join(preview):
                        raise ProviderError("模型流式正文与最终回答不一致，请重试")
                else:
                    response = await run_db(learning.generate, profile, system, prompt, structured=True)
                    learning.apply_answer(result, response, evidence)
            else:
                yield "delta", {"text": result["answer"]}
            yield "status", {"message": "正在核对引用并保存…"}
            if evidence and data.calculation:
                result["calculation"] = await run_in_threadpool(
                    learning.bounded, "calculate", data.calculation.model_dump())
            # Cancellation before this point never creates a successful conversation entry.
            def save():
                nonlocal saved
                completed = learning.save_chat(db, runtime.app if runtime else app,
                                               versions, conversation, result, user_id, data, execution=execution)
                # Set inside the worker: cancellation may arrive after commit but before await resumes.
                saved = True
                return completed

            result = await run_db(save)
            yield "done", result
        except (ProviderError, HTTPException) as exc:
            db.rollback()
            await run_db(record_failure, db, app_id, user_id, runtime,
                         getattr(exc, "execution", {}).get("state", "failed"), getattr(exc, "code", "scope_changed"))
            yield "error", {"message": exc.detail if isinstance(exc, HTTPException) else str(exc),
                            "code": exc.status_code if isinstance(exc, HTTPException) else 503,
                            "error_code": getattr(exc, "code", "scope_changed")}
        except (asyncio.CancelledError, GeneratorExit):
            if not saved:
                with anyio.CancelScope(shield=True):
                    await run_db(record_failure, db, app_id, user_id, runtime, "cancelled", "cancelled")
            raise
        except Exception:
            db.rollback()
            logger.error("Streaming chat failed", exc_info=False)
            await run_db(record_failure, db, app_id, user_id, runtime)
            yield "error", {"message": "回答未能完成，请稍后重试", "code": 500}


async def chat_events(app_id, user_id, data, *, stream=True, use_graph=True):
    """A single persisted execution per request ID; duplicate clients only observe it."""
    from .admission import drain, reset_progress, set_progress
    from .chat_runs import claim_run, finish_run, get_run, start_run

    claim = None
    with Session(engine(), expire_on_commit=False) as db:
        def reserve():
            nonlocal claim
            claim = claim_run(db, app_id, user_id, data)
            return claim

        try:
            await run_db(reserve)
        except (asyncio.CancelledError, GeneratorExit):
            if claim is not None and claim.created:
                with anyio.CancelScope(shield=True):
                    await run_db(finish_run, claim.run_id, claim.owner_token, "cancelled",
                                 {"code": "cancelled", "message": "请求已取消。"})
            raise
        except HTTPException as exc:
            yield "error", {"message": exc.detail, "code": exc.status_code,
                            "error_code": getattr(exc, "code", "request_conflict")}
            return

    def lookup():
        with Session(engine(), expire_on_commit=False) as db:
            return get_run(db, app_id, user_id, claim.request_id)

    if not claim.created:
        while True:
            try:
                view = await run_db(lookup)
            except HTTPException as exc:
                yield "error", {"message": exc.detail, "code": exc.status_code}
                return
            if view["state"] == "succeeded":
                yield "done", view["result"]
                return
            if view["state"] in {"failed", "cancelled"}:
                error = view.get("error") or {}
                yield "error", {"message": error.get("message", "这次请求未完成，请重新发送问题。"),
                                "error_code": error.get("code", "request_ended"), "code": 409}
                return
            yield "status", {"message": "这次请求已在处理中，正在等待已有结果…", "state": view["state"]}
            await asyncio.sleep(.5)

    queue = asyncio.Queue(maxsize=1)
    loop = asyncio.get_running_loop()
    closing = False

    def progress(value):
        # The same callback can be used by provider threads and async child agents.
        def publish():
            if not closing and not queue.full():
                queue.put_nowait(("status", value))
        loop.call_soon_threadsafe(publish)

    async def work():
        token, progress_token = providers.model_limits.set(True), set_progress(progress)
        try:
            async with start_run(claim.run_id, claim.owner_token, on_wait=progress):
                async with aclosing(_answer_events(app_id, user_id, data, execution=claim,
                                                   stream=stream, use_graph=use_graph)) as source:
                    async for kind, value in source:
                        if kind == "error":
                            error = HTTPException(value.get("code", 503), value["message"])
                            error.code = value.get("error_code", "execution_failed")
                            raise error
                        await queue.put((kind, value))
        except asyncio.CancelledError:
            if not closing:
                await queue.put(("error", {"code": 409, "error_code": "cancelled", "message": "这次请求已取消。"}))
        except HTTPException as exc:
            if not closing:
                await queue.put(("error", {"code": exc.status_code, "error_code": getattr(exc, "code", "execution_failed"),
                                         "message": exc.detail}))
        except Exception:
            logger.error("Chat request failed", exc_info=False)
            if not closing:
                await queue.put(("error", {"code": 500, "message": "回答未能完成，请稍后重试"}))
        finally:
            reset_progress(progress_token)
            providers.model_limits.reset(token)
            if not closing:
                await queue.put(None)

    worker = asyncio.create_task(work())
    try:
        yield "request", {"request_id": claim.request_id, "state": claim.state, "reused": False}
        while True:
            event = await queue.get()
            if event is None:
                await worker
                break
            yield event
    finally:
        closing = True
        with anyio.CancelScope(shield=True):
            await drain(worker)
            # Covers disconnect before the worker even entered start_run; success is immutable.
            await run_db(finish_run, claim.run_id, claim.owner_token, "cancelled",
                         {"code": "cancelled", "message": "请求已取消，可重新发送问题。"})


async def chat_result(app_id, user_id, data, *, use_graph=True):
    async with aclosing(chat_events(app_id, user_id, data, stream=False, use_graph=use_graph)) as source:
        async for kind, value in source:
            if kind == "done":
                return value
            if kind == "error":
                raise HTTPException(value.get("code", 503), value["message"])
    raise HTTPException(503, "本次回答未完成，请查询请求状态。")


async def event_stream(events, heartbeat_seconds=10):
    """Keep the proxy connection alive while retrieval or provider thinking is pending."""
    pending = None
    try:
        while True:
            if pending is None:
                pending = asyncio.create_task(anext(events))
            ready, _ = await asyncio.wait({pending}, timeout=heartbeat_seconds)
            if not ready:
                yield ": keep-alive\n\n"
                continue
            try:
                kind, value = pending.result()
            except StopAsyncIteration:
                break
            pending = None
            yield f"event: {kind}\ndata: {json.dumps(value, ensure_ascii=False)}\n\n"
    finally:
        # ASGI disconnect cancellation must still allow the upstream socket/session to close.
        with anyio.CancelScope(shield=True):
            if pending is not None:
                pending.cancel()
                with suppress(asyncio.CancelledError, StopAsyncIteration):
                    await pending
            await events.aclose()
