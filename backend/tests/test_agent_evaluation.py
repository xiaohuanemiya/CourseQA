import asyncio
import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import Base, engine
from app.models import Application, Knowledge, KnowledgeBase, Message, ModelProfile, Release, User
from app.schemas import ApplicationInput
from app.services import agent_evaluation as evaluation, learning, providers
from app.services.retrieval import Evidence
from app.services.providers import ProviderError


@pytest.fixture
def platform(tmp_path, monkeypatch):
    """Offline evaluation needs a database, not the HTTP server or model transport."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'evaluation.db'}")
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    settings.cache_clear()
    engine.cache_clear()
    Base.metadata.create_all(engine())
    with Session(engine()) as db:
        user = User(username="admin", password_hash="unused", role="admin")
        generation = ModelProfile(name="test", provider="demo", model="test", purpose="generation")
        embedding = ModelProfile(name="embedding", provider="demo", model="hash", purpose="embedding", dimension=128)
        db.add_all([user, generation, embedding])
        db.flush()
        kb = KnowledgeBase(name="导数", extraction_profile_id=generation.id)
        db.add(kb)
        db.flush()
        node = Knowledge(kb_id=kb.id, kind="formula", title="导数知识", content="本例设 x 为实数", reviewed=True)
        db.add(node)
        db.flush()
        release = Release(kb_id=kb.id, revision=1, author_id=user.id, checksum="fixture", embedding_signature="fixture",
            snapshot={"nodes": [{"id": node.id, "kb_id": kb.id, "title": node.title, "content": node.content,
                                 "kind": node.kind, "attributes": {}, "sources": []}], "edges": [], "questions": []})
        db.add(release)
        db.flush()
        kb.current_release_id = release.id
        published = ApplicationInput(name="评测", model_profile_id=generation.id,
                                     embedding_profile_id=embedding.id, kb_ids=[kb.id]).model_dump()
        app = Application(draft=published, published=published, version=1, active=True)
        db.add(app)
        db.commit()
        data = {"bundles": [{"app": {"id": app.id}}], "generation": {"id": generation.id}}
    yield data
    engine().dispose()
    engine.cache_clear()
    settings.cache_clear()


def reviewed_row(index=1):
    return {"id": f"q-{index}", "group_id": f"family-{index}", "type": "single", "split": "test",
            "question": f"导数知识的适用条件是什么？{index}", "answerable": True,
            "gold_answer": "本例设 x 为实数", "evidence_titles": ["导数知识"], "reviewed": True}


def setup_model(platform):
    with Session(engine()) as db:
        profile = db.get(ModelProfile, platform["generation"]["id"])
        profile.provider = "openai"
        user_id = db.scalar(select(User.id).where(User.username == "admin"))
        db.commit()
    return platform["bundles"][0]["app"]["id"], user_id


def answer(usage=None):
    return {"status": "answered", "answer": "本例设 x 为实数 [1]。", "citations": [{"title": "导数知识"}],
            "retrieved_titles": ["导数知识"],
            "agent": {"model_calls": 1, "tool_calls": 0, "subagents": 0, "elapsed_ms": 10,
                      "usage": usage, "trace": []}}


def test_annotation_candidates_are_unreviewed_and_never_invent_gold(platform):
    with Session(engine()) as db:
        metadata, releases = evaluation.published_context(db, platform["bundles"][0]["app"]["id"])
        rows = evaluation.candidate_questions(releases)
    assert len(rows) == 40
    assert all(row["reviewed"] is False and not row["gold_answer"] and not row["evidence_titles"] for row in rows)
    assert all(row["source_candidates"][0]["title"] == "导数知识" for row in rows)
    assert all(row["question"] == "" for row in rows if row["type"] in {"calculation", "unanswerable"})
    assert metadata["release_ids"] and metadata["knowledge_sha256"]
    assert "secret" not in json.dumps(metadata)
    with pytest.raises(ValueError, match="尚未人工审核"):
        evaluation.validate_agent_dataset(rows, split="dev")
    with pytest.raises(ValueError, match="30–50"):
        evaluation.candidate_questions(releases, count=10)


def test_dataset_requires_review_labels_and_preserves_group_isolation():
    row = reviewed_row()
    assert evaluation.validate_agent_dataset([row], allow_small=True) == [row]
    with pytest.raises(ValueError, match="30–50"):
        evaluation.validate_agent_dataset([row])
    with pytest.raises(ValueError, match="尚未人工审核"):
        evaluation.validate_agent_dataset([{**row, "reviewed": False}], allow_small=True)
    with pytest.raises(ValueError, match="标准答案"):
        evaluation.validate_agent_dataset([{**row, "gold_answer": ""}], allow_small=True)
    with pytest.raises(ValueError, match="泄漏"):
        evaluation.validate_agent_dataset([row, {**row, "id": "dev-q", "split": "dev"}], allow_small=True)


def test_same_questions_modes_rotate_and_no_records_or_configs_change(platform):
    app_id, user_id = setup_model(platform)
    calls = []

    async def evaluate(db, app_id, user_id, data, *, mode):
        assert data.conversation_id is None
        calls.append((data.message, mode))
        return answer({"prompt_tokens": 10, "completion_tokens": 4} if mode != "tools" else None)

    with Session(engine(), autoflush=False) as db:
        original = copy.deepcopy(db.get(Application, app_id).published)
        report = asyncio.run(evaluation.evaluate_dataset(db, app_id, user_id,
            [reviewed_row(), reviewed_row(2)], allow_small=True, evaluator=evaluate))
        assert db.get(Application, app_id).published == original
        assert db.scalar(select(func.count()).select_from(Message)) == 0
    assert [mode for _, mode in calls] == ["baseline", "tools", "multi_agent", "tools", "multi_agent", "baseline"]
    assert len({question for question, _ in calls}) == 2
    assert report["complete"]
    assert report["metadata"]["dataset_sha256"] and report["metadata"]["model"]["provider"] == "openai"
    for mode in evaluation.MODES:
        output = report["modes"][mode]
        assert output["metrics"]["accuracy"] is None
        assert output["metrics"]["citation_support_rate"] is None
        assert output["metrics"]["execution"]["model_calls"]["total"] == 2
        assert output["metrics"]["evidence_recall"] == 1
        assert output["metrics"]["evidence_recall_at_8"] is None
    assert report["modes"]["tools"]["metrics"]["usage_totals"]["prompt_tokens"]["total"] is None
    assert report["modes"]["baseline"]["metrics"]["usage_totals"]["prompt_tokens"]["total"] == 20


def test_failures_keep_partial_metrics_and_unknown_usage_without_leaking(platform):
    app_id, user_id = setup_model(platform)
    checkpoints = []

    async def evaluate(db, app_id, user_id, data, *, mode):
        if mode == "tools":
            exc = ProviderError("private upstream data must not appear")
            exc.execution = {"model_calls": 2, "tool_calls": 1, "subagents": 0,
                             "usage": {"prompt_tokens": 12, "completion_tokens": None},
                             "trace": [{"kind": "tool_failed"}]}
            raise exc
        result = answer()
        del result["retrieved_titles"]
        return result

    with Session(engine()) as db:
        report = asyncio.run(evaluation.evaluate_dataset(db, app_id, user_id, [reviewed_row()],
            allow_small=True, evaluator=evaluate, progress=lambda current: checkpoints.append(copy.deepcopy(current))))
    assert len(checkpoints) == 4 and not checkpoints[0]["complete"] and checkpoints[-1]["complete"]
    assert "private upstream" not in json.dumps(report)
    failed = report["modes"]["tools"]
    assert failed["results"][0]["status"] == "error"
    assert failed["metrics"]["error_count"] == 1 and failed["metrics"]["refusal_recall"] is None
    assert failed["metrics"]["execution"]["model_calls"]["total"] == 2
    assert failed["metrics"]["usage_totals"]["completion_tokens"]["total"] is None
    assert failed["results"][0]["execution"]["trace"] == [{"kind": "tool_failed"}]
    assert report["modes"]["baseline"]["metrics"]["evidence_recall"] is None


def test_demo_or_out_of_scope_labels_do_not_call_model(platform):
    app_id = platform["bundles"][0]["app"]["id"]
    called = []

    async def evaluate(*args, **kwargs):
        called.append(True)
        return answer()

    with Session(engine()) as db:
        user_id = db.scalar(select(User.id).where(User.username == "admin"))
        with pytest.raises(ValueError, match="真实生成模型"):
            asyncio.run(evaluation.evaluate_dataset(db, app_id, user_id, [reviewed_row()],
                        allow_small=True, evaluator=evaluate))
    app_id, user_id = setup_model(platform)
    with Session(engine()) as db:
        with pytest.raises(ValueError, match="不在此应用"):
            asyncio.run(evaluation.evaluate_dataset(db, app_id, user_id,
                        [{**reviewed_row(), "evidence_titles": ["积分知识"]}], allow_small=True, evaluator=evaluate))
    assert not called


def test_version_change_stops_comparison_before_mixing_versions(platform):
    app_id, user_id = setup_model(platform)
    calls = []

    async def evaluate(*args, **kwargs):
        calls.append(kwargs["mode"])
        with Session(engine()) as other:
            app = other.get(Application, app_id)
            app.version += 1
            other.commit()
        return answer()

    with Session(engine()) as db:
        with pytest.raises(ValueError, match="版本已变化"):
            asyncio.run(evaluation.evaluate_dataset(db, app_id, user_id, [reviewed_row()],
                        allow_small=True, evaluator=evaluate))
    assert calls == ["baseline"]


def test_cli_help_does_not_require_database_or_model():
    script = Path(__file__).resolve().parents[2] / "scripts" / "evaluate_agents.py"
    for arguments in (["--help"], ["prepare", "--help"], ["run", "--help"], ["score", "--help"]):
        result = subprocess.run([sys.executable, str(script), *arguments], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert "usage:" in result.stdout


def test_scoring_requires_reviewed_independent_labels_and_preserves_unknowns():
    result = {"id": "q-1", "status": "answered", "answerable": True, "gold_evidence": ["e"],
              "retrieved_titles": ["e"], "retrieval_recorded": True, "latency_seconds": 1,
              "execution": {"model_calls": 1, "tool_calls": 0, "subagents": 0}, "usage": None, "error": None}
    report = {"results": [result], "metadata": {"app_version": 1}}
    labels = [{"id": "q-1", "reviewed": True, "correct": False, "citations_supported": True}]
    scored = evaluation.score_report(report, labels)
    assert scored["metrics"]["accuracy"] == 0
    assert scored["metrics"]["citation_support_rate"] == 1
    assert scored["metrics"]["evidence_recall_at_8"] is None
    assert scored["metrics"]["usage_totals"]["prompt_tokens"]["total"] is None
    assert scored["metadata"] == report["metadata"] and "metrics" not in report
    assert scored["judgments_sha256"]
    with pytest.raises(ValueError, match="reviewed"):
        evaluation.score_report(report, [{**labels[0], "reviewed": False}])
    with pytest.raises(ValueError, match="重复"):
        evaluation.score_report(report, labels + labels)


def test_real_evaluator_contract_uses_one_model_turn_and_detached_mode_options(platform, monkeypatch):
    app_id, user_id = setup_model(platform)
    definitions_seen = []

    def retrieve(db, app, releases, query, use_graph=True):
        release, snapshot = releases[0]
        return Evidence([{**snapshot["nodes"][0], "release_id": release.id, "path": [], "score": 1}], {})

    async def stream(profile, messages, definitions):
        definitions_seen.append({tool["function"]["name"] for tool in definitions})
        content = json.dumps({"status": "answered", "answer": "本例设 x 为实数 [1]。", "citation_ids": ["1"]})
        yield "delta", content
        yield "done", {"message": {"role": "assistant", "content": content, "tool_calls": []},
                       "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

    monkeypatch.setattr(learning, "retrieve", retrieve)
    monkeypatch.setattr(providers, "stream_tool_turn", stream)
    with Session(engine(), autoflush=False) as db:
        original = copy.deepcopy(db.get(Application, app_id).published)
        report = asyncio.run(evaluation.evaluate_dataset(db, app_id, user_id, [reviewed_row()], allow_small=True))
        assert db.get(Application, app_id).published == original
        assert db.scalar(select(func.count()).select_from(Message)) == 0
    assert definitions_seen[0] == set()
    assert "calculate" in definitions_seen[1] and "spawn_agent" not in definitions_seen[1]
    assert "spawn_agent" in definitions_seen[2]
    for mode in evaluation.MODES:
        result = report["modes"][mode]["results"][0]
        assert result["status"] == "answered"
        assert result["execution"]["model_calls"] == 1
        assert result["execution"]["subagents"] == result["execution"]["tool_calls"] == 0
        assert result["retrieved_titles"] == ["导数知识"]
        assert result["usage"] == {"prompt_tokens": 10, "completion_tokens": 5}


def test_real_evaluator_failure_is_reported_without_saving_student_messages(platform, monkeypatch):
    app_id, user_id = setup_model(platform)

    def retrieve(db, app, releases, query, use_graph=True):
        release, snapshot = releases[0]
        return Evidence([{**snapshot["nodes"][0], "release_id": release.id, "path": [], "score": 1}], {})

    async def stream(profile, messages, definitions):
        raise ProviderError("secret upstream content", code="invalid_response")
        yield  # Establish an async iterator without contacting a model.

    monkeypatch.setattr(learning, "retrieve", retrieve)
    monkeypatch.setattr(providers, "stream_tool_turn", stream)
    with Session(engine(), autoflush=False) as db:
        report = asyncio.run(evaluation.evaluate_dataset(db, app_id, user_id, [reviewed_row()], allow_small=True))
        assert db.scalar(select(func.count()).select_from(Message)) == 0
    assert "secret upstream content" not in json.dumps(report)
    for mode in evaluation.MODES:
        output = report["modes"][mode]
        assert output["metrics"]["error_count"] == 1
        assert output["results"][0]["execution"]["model_calls"] == 1
        assert output["metrics"]["usage_totals"]["prompt_tokens"]["total"] is None


def test_real_evaluator_records_zero_calls_for_explicit_missing_document(platform, monkeypatch):
    app_id, user_id = setup_model(platform)
    monkeypatch.setattr(learning, "retrieve", lambda *args, **kwargs: Evidence([], {"status": "not_found"}))

    async def never_call(*args, **kwargs):
        raise AssertionError("Explicit document misses should not invoke a model")
        yield

    monkeypatch.setattr(providers, "stream_tool_turn", never_call)
    with Session(engine(), autoflush=False) as db:
        report = asyncio.run(evaluation.evaluate_dataset(db, app_id, user_id, [reviewed_row()], allow_small=True))
        assert db.scalar(select(func.count()).select_from(Message)) == 0
    for mode in evaluation.MODES:
        result = report["modes"][mode]["results"][0]
        assert result["status"] == "insufficient_evidence"
        assert result["execution"]["model_calls"] == result["execution"]["tool_calls"] == 0
        assert result["usage"] == {"prompt_tokens": 0, "completion_tokens": 0}
