"""End-to-end request identities: replay, isolation, cancellation, and races."""
import asyncio
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import engine
from app.models import ChatRun, Conversation, Message, ModelProfile
from app.services import chat_runs, learning, streaming
from app.services.providers import ProviderError
from .conftest import post
from .test_streaming import events


ANSWER = "仅限当前请求查看的课程依据 REPLAY_SECRET_MARKER [1]。"
PAYLOAD = {"status": "answered", "answer": ANSWER, "citation_ids": ["1"]}


def request(message="导数知识"):
    return {"request_id": str(uuid.uuid4()), "message": message}


def counts():
    with Session(engine()) as db:
        return {model.__tablename__: db.scalar(select(func.count()).select_from(model))
                for model in (ChatRun, Conversation, Message)}


def model(monkeypatch, platform, *, fail=False, started=None, release=None, closed=None):
    with Session(engine()) as db:
        db.get(ModelProfile, platform["generation"]["id"]).provider = "openai"
        db.commit()
    calls = []

    def generate(*args, **kwargs):
        calls.append("sync")
        try:
            if started:
                started.set()
            if release:
                assert release.wait(8), "test did not release model execution"
            if fail:
                raise ProviderError("模型本次暂不可用", code="provider_connection", retryable=True)
            return dict(PAYLOAD)
        finally:
            if closed:
                closed.set()

    async def generate_stream(*args, **kwargs):
        calls.append("stream")
        try:
            if started:
                started.set()
            deadline = time.monotonic() + 8
            while release and not release.is_set():
                assert time.monotonic() < deadline, "test did not release model execution"
                await asyncio.sleep(.01)
            if fail:
                raise ProviderError("模型本次暂不可用", code="provider_connection", retryable=True)
            value = json.dumps(PAYLOAD)
            for index in range(0, len(value), 11):
                yield value[index:index + 11]
        finally:
            if closed:
                closed.set()

    monkeypatch.setattr(learning, "generate", generate)
    monkeypatch.setattr(streaming, "stream_generate", generate_stream)
    return calls


def test_repeated_sync_request_reuses_exact_result_without_model_or_history_duplication(client, platform, monkeypatch):
    app_id = platform["bundles"][0]["app"]["id"]
    calls = model(monkeypatch, platform)
    data = request()
    first = post(client, f"/apps/{app_id}/chat", data)
    second = post(client, f"/apps/{app_id}/chat", data)
    assert first == second
    assert first["conversation_id"] == second["conversation_id"]
    assert len(calls) == 1
    assert counts() == {"chat_runs": 1, "conversations": 1, "messages": 1}
    status = client.get(f"/api/apps/{app_id}/chat/requests/{data['request_id']}")
    assert status.status_code == 200
    assert status.json()["request_id"] == data["request_id"]
    assert status.json()["state"] == "succeeded"
    assert status.json()["result"] == first


@pytest.mark.parametrize("suffix", ["", "/stream"])
def test_waiting_for_model_holds_no_authentication_or_retrieval_connection(client, platform, monkeypatch, suffix):
    app_id = platform["bundles"][0]["app"]["id"]
    model(monkeypatch, platform)
    observed = []

    def generate(*args, **kwargs):
        observed.append(engine().pool.checkedout())
        return dict(PAYLOAD)

    async def generate_stream(*args, **kwargs):
        observed.append(engine().pool.checkedout())
        yield json.dumps(PAYLOAD)

    monkeypatch.setattr(learning, "generate", generate)
    monkeypatch.setattr(streaming, "stream_generate", generate_stream)
    response = client.post(f"/api/apps/{app_id}/chat{suffix}", json=request(),
                           headers={"X-Requested-With": "calculus-studio"})
    assert response.status_code == 200
    assert observed == [0]
    if suffix:
        assert any(kind == "done" for kind, _ in events(response))
    else:
        assert response.json()["status"] == "answered"


@pytest.mark.parametrize("changed", [{"message": "完全不同的问题"},
                                     {"calculation": {"operation": "differentiate", "expression": "x^2"}}])
def test_same_identity_cannot_be_reused_for_different_payload(client, platform, monkeypatch, changed):
    app_id = platform["bundles"][0]["app"]["id"]
    calls = model(monkeypatch, platform)
    data = request()
    post(client, f"/apps/{app_id}/chat", data)
    response = client.post(f"/api/apps/{app_id}/chat", json={**data, **changed})
    assert response.status_code == 409
    assert len(calls) == 1
    assert counts()["messages"] == counts()["chat_runs"] == 1


def test_request_identity_and_read_cancel_endpoints_are_user_and_application_scoped(client, platform, monkeypatch):
    first_app, second_app = [bundle["app"]["id"] for bundle in platform["bundles"]]
    calls = model(monkeypatch, platform)
    data = request()
    admin_result = post(client, f"/apps/{first_app}/chat", data)
    assert client.get(f"/api/apps/{second_app}/chat/requests/{data['request_id']}").status_code == 404
    assert client.post(f"/api/apps/{second_app}/chat/requests/{data['request_id']}/cancel").status_code == 404
    other_app = post(client, f"/apps/{second_app}/chat", {**data, "message": "积分知识"})
    post(client, "/auth/register", {"username": "request-owner", "password": "strong-test-password"})
    assert client.get(f"/api/apps/{first_app}/chat/requests/{data['request_id']}").status_code == 404
    assert client.post(f"/api/apps/{first_app}/chat/requests/{data['request_id']}/cancel").status_code == 404
    student_result = post(client, f"/apps/{first_app}/chat", data)
    assert len({admin_result["conversation_id"], other_app["conversation_id"], student_result["conversation_id"]}) == 3
    student_status = client.get(f"/api/apps/{first_app}/chat/requests/{data['request_id']}").json()
    assert student_status["result"] == student_result
    assert len(calls) == 3
    assert counts() == {"chat_runs": 3, "conversations": 3, "messages": 3}


def test_withdrawn_evidence_redacts_get_and_replay_results_without_regeneration(client, platform, monkeypatch):
    bundle = platform["bundles"][0]
    app_id = bundle["app"]["id"]
    calls = model(monkeypatch, platform)
    data = request()
    first = post(client, f"/apps/{app_id}/chat", data)
    assert ANSWER == first["answer"]
    withdrawal = client.delete(f"/api/admin/knowledge-bases/{bundle['kb']['id']}/knowledge/{bundle['node']['id']}")
    assert withdrawal.status_code == 200
    status = client.get(f"/api/apps/{app_id}/chat/requests/{data['request_id']}")
    assert status.status_code == 200
    assert status.json()["state"] == "succeeded"
    assert status.json()["result"]["status"] == "outdated"
    assert status.json()["result"]["citations"] == []
    assert "REPLAY_SECRET_MARKER" not in status.text
    replay = client.post(f"/api/apps/{app_id}/chat", json=data)
    assert replay.status_code == 200
    assert replay.json()["status"] == "outdated"
    assert "REPLAY_SECRET_MARKER" not in replay.text
    assert len(calls) == 1 and counts()["messages"] == 1


@pytest.mark.parametrize("first_stream", [False, True])
def test_completed_request_stream_replay_emits_only_done(client, platform, monkeypatch, first_stream):
    app_id = platform["bundles"][0]["app"]["id"]
    calls = model(monkeypatch, platform)
    data = request()
    if first_stream:
        first_events = events(client.post(f"/api/apps/{app_id}/chat/stream", json=data))
        assert first_events[-1][0] == "done"
        result = first_events[-1][1]
    else:
        result = post(client, f"/apps/{app_id}/chat", data)
    replay = events(client.post(f"/api/apps/{app_id}/chat/stream", json=data))
    assert replay == [("done", result)]
    assert len(calls) == 1 and counts()["messages"] == 1


def test_completed_request_cannot_be_cancelled(client, platform, monkeypatch):
    app_id = platform["bundles"][0]["app"]["id"]
    model(monkeypatch, platform)
    data = request()
    result = post(client, f"/apps/{app_id}/chat", data)
    cancelled = post(client, f"/apps/{app_id}/chat/requests/{data['request_id']}/cancel")
    assert cancelled["state"] == "succeeded"
    assert cancelled["result"] == result
    assert counts()["messages"] == 1


@pytest.mark.parametrize("stream", [False, True])
def test_failed_identity_is_not_reexecuted_on_sync_or_stream_retry(client, platform, monkeypatch, stream):
    app_id = platform["bundles"][0]["app"]["id"]
    calls = model(monkeypatch, platform, fail=True)
    data = request()
    first = client.post(f"/api/apps/{app_id}/chat", json=data)
    assert first.status_code == 503
    run = client.get(f"/api/apps/{app_id}/chat/requests/{data['request_id']}").json()
    assert run["state"] == "failed" and run["result"] is None
    replay = client.post(f"/api/apps/{app_id}/chat" + ("/stream" if stream else ""), json=data)
    if stream:
        frames = events(replay)
        assert frames[-1][0] == "error"
        assert frames[-1][1]["code"] == 409
        assert not any(kind == "done" for kind, _ in frames)
    else:
        assert replay.status_code == 409
    assert len(calls) == 1
    assert counts() == {"chat_runs": 1, "conversations": 0, "messages": 0}


def test_concurrent_identical_requests_wait_for_single_execution(client, platform, monkeypatch):
    app_id = platform["bundles"][0]["app"]["id"]
    started, release, reused = threading.Event(), threading.Event(), threading.Event()
    calls = model(monkeypatch, platform, started=started, release=release)
    original_claim = chat_runs.claim_run

    def claim(*args, **kwargs):
        outcome = original_claim(*args, **kwargs)
        if not outcome.created:
            reused.set()
        return outcome

    monkeypatch.setattr(chat_runs, "claim_run", claim)
    data = request()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(client.post, f"/api/apps/{app_id}/chat", json=data)
        try:
            assert started.wait(5), "first model invocation did not start"
            second = pool.submit(client.post, f"/api/apps/{app_id}/chat", json=data)
            assert reused.wait(5), "concurrent duplicate did not join the existing request"
            state = client.get(f"/api/apps/{app_id}/chat/requests/{data['request_id']}").json()
            assert state["state"] in {"queued", "running"} and state["result"] is None
            assert len(calls) == 1
            release.set()
            responses = [first.result(timeout=5), second.result(timeout=5)]
        finally:
            release.set()
    assert all(response.status_code == 200 for response in responses)
    assert responses[0].json() == responses[1].json()
    assert len(calls) == 1
    assert counts() == {"chat_runs": 1, "conversations": 1, "messages": 1}


def test_cancelling_running_stream_stops_model_and_identity_cannot_restart(client, platform, monkeypatch):
    app_id = platform["bundles"][0]["app"]["id"]
    started, release, closed = threading.Event(), threading.Event(), threading.Event()
    calls = model(monkeypatch, platform, started=started, release=release, closed=closed)
    data = request()
    with ThreadPoolExecutor(max_workers=1) as pool:
        stream = pool.submit(client.post, f"/api/apps/{app_id}/chat/stream", json=data)
        try:
            assert started.wait(5), "model stream did not start"
            running = client.get(f"/api/apps/{app_id}/chat/requests/{data['request_id']}").json()
            assert running["state"] == "running"
            cancelled = post(client, f"/apps/{app_id}/chat/requests/{data['request_id']}/cancel")
            assert cancelled["state"] == "cancelled" and cancelled["result"] is None
            assert closed.wait(4), "cancel did not close upstream model stream"
            response = stream.result(timeout=5)
            if response.status_code == 200:
                assert not any(kind == "done" for kind, _ in events(response))
        finally:
            release.set()
    assert client.post(f"/api/apps/{app_id}/chat", json=data).status_code == 409
    replay = events(client.post(f"/api/apps/{app_id}/chat/stream", json=data))
    assert replay[-1][0] == "error" and replay[-1][1]["code"] == 409
    assert len(calls) == 1
    assert counts() == {"chat_runs": 1, "conversations": 0, "messages": 0}
