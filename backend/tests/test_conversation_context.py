import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.models import Application, Conversation, Document, Knowledge, KnowledgeBase, Message, Release, User
from app.services import conversation_context as context
from app.services.knowledge import valid_snapshot


@pytest.fixture
def history_db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = User(username="learner", password_hash="unused")
        other_user = User(username="other", password_hash="unused")
        kb = KnowledgeBase(name="微积分")
        db.add_all([user, other_user, kb])
        db.flush()
        document = Document(kb_id=kb.id, filename="2021-2022微积分月考.pdf", path="unused", checksum="test")
        db.add(document)
        db.flush()
        node = Knowledge(kb_id=kb.id, kind="formula", title="链式法则", content="有效发布正文",
                         sources=[{"document_id": document.id, "locator": "第2页"}])
        db.add(node)
        db.flush()
        snapshot = {"nodes": [{"id": node.id, "title": node.title, "sources": node.sources}],
                    "edges": [], "questions": []}
        release = Release(kb_id=kb.id, revision=1, snapshot=snapshot, embedding_signature="unused",
                          checksum="test", author_id=user.id)
        app = Application(draft={}, published={"kb_ids": [kb.id]}, version=2, active=True)
        db.add_all([release, app])
        db.flush()
        conversation = Conversation(user_id=user.id, app_id=app.id, title="连续追问")
        db.add(conversation)
        db.commit()
        yield SimpleNamespace(db=db, app=app, kb=kb, release=release, node=node, document=document,
                              user=user, other_user=other_user, conversation=conversation)
    engine.dispose()


def add_turn(state, question="解释链式法则", answer="第一步设 u=x²，第二步对复合函数求导 [1]。", **overrides):
    result = {"answer": answer, "status": "answered", "app_version": state.app.version,
              "versions": {state.kb.id: state.release.id},
              "citations": [{"knowledge_id": state.node.id, "release_id": state.release.id,
                             "documents": [{"id": "forged", "filename": "另一应用的秘密.pdf"}]}],
              "retrieval": {"history": "嵌套的历史结果不可再次复制"}}
    result.update(overrides.pop("result", {}))
    message = Message(conversation_id=overrides.pop("conversation_id", state.conversation.id),
                      question=question, result=result, **overrides)
    state.db.add(message)
    state.db.flush()
    return message


def build(state, question="为什么第二步成立？", conversation=None):
    return context.build_context(state.db, state.app,
        [(state.release, valid_snapshot(state.db, state.release))],
        conversation or state.conversation, question)


def test_standalone_questions_keep_retrieval_and_include_only_bounded_dialogue(history_db):
    state = history_db
    message = add_turn(state)
    actual = build(state, "定积分的几何意义是什么？")
    assert actual["retrieval_query"] == "定积分的几何意义是什么？"
    assert not actual["followup_applied"]
    assert actual["recent_turns"][0]["message_id"] == message.id
    assert "第二步" in actual["recent_turns"][0]["answer"]
    assert "[历史引用 1]" in actual["recent_turns"][0]["answer"]
    assert "[1]" not in actual["recent_turns"][0]["answer"]
    assert "嵌套的历史" not in json.dumps(actual, ensure_ascii=False)
    assert "另一应用的秘密" not in json.dumps(actual, ensure_ascii=False)
    assert "不是事实证据" in actual["policy"]
    assert message.result["answer"].endswith("[1]。")


@pytest.mark.parametrize("question", ["为什么第二步成立？", "换一种解法", "继续", "再详细一点", "请继续讲解",
                                      "Can you explain that step?"])
def test_followup_uses_previous_question_and_current_document_metadata(history_db, question):
    state = history_db
    add_turn(state, "月考中的链式法则题怎么做？")
    actual = build(state, question)
    assert actual["followup_applied"]
    assert actual["retrieval_query"] == question
    assert actual["topic_hint"] == "月考中的链式法则题怎么做？"
    assert actual["inherited_document_ids"] == [state.document.id]
    assert "另一应用的秘密" not in json.dumps(actual, ensure_ascii=False)


def test_new_explicit_document_takes_precedence_over_previous_paper(history_db):
    state = history_db
    add_turn(state, "2021-2022微积分月考考了什么？")
    question = "那2023-2024微积分月考呢？"
    actual = build(state, question)
    assert actual["retrieval_query"] == question
    assert not actual["followup_applied"]


def test_chained_followup_keeps_original_topic(history_db):
    state = history_db
    origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
    add_turn(state, "求复合函数 sin(x²) 的导数", created_at=origin)
    add_turn(state, "解释第二步", created_at=origin + timedelta(minutes=1))
    actual = build(state, "换一种解法")
    assert actual["retrieval_query"] == "换一种解法"
    assert "解释第二步" in actual["topic_hint"]
    assert "sin(x²)" in actual["topic_hint"]


def test_context_is_isolated_by_conversation_app_and_owner(history_db):
    state = history_db
    own = add_turn(state)
    another = Conversation(user_id=state.other_user.id, app_id=state.app.id, title="其他人的会话")
    state.db.add(another)
    state.db.flush()
    add_turn(state, "另一个用户的秘密问题", answer="另一个用户的秘密答案", conversation_id=another.id)
    other_app = Application(draft={}, published={"kb_ids": [state.kb.id]}, version=2)
    state.db.add(other_app)
    state.db.flush()
    third = Conversation(user_id=state.user.id, app_id=other_app.id, title="其他应用会话")
    state.db.add(third)
    state.db.flush()
    add_turn(state, "另一个应用的秘密问题", conversation_id=third.id)
    actual = build(state)
    assert actual["history_count"] == 1
    assert [turn["message_id"] for turn in actual["recent_turns"]] == [own.id]
    assert "秘密" not in json.dumps(actual, ensure_ascii=False)
    with pytest.raises(HTTPException) as error:
        build(state, conversation=third)
    assert error.value.status_code == 404
    forged = SimpleNamespace(id=another.id, app_id=state.app.id, user_id=state.user.id)
    with pytest.raises(HTTPException):
        build(state, conversation=forged)


@pytest.mark.parametrize("change", ["application", "knowledge", "missing_metadata"])
def test_old_versions_cannot_reintroduce_history_into_a_new_scope(history_db, change):
    state = history_db
    turn = add_turn(state, "旧库独有的秘密题目", answer="旧版本答案不得沿用")
    if change == "application":
        state.app.version += 1
    elif change == "knowledge":
        turn.result = {**turn.result, "versions": {state.kb.id: "old-release"}}
    else:
        turn.result = {"answer": "旧版本答案不得沿用"}
    actual = build(state)
    assert actual["recent_turns"] == actual["summary"] == []
    assert actual["excluded_version_turns"] == actual["excluded_assistant_turns"] == 1
    assert not actual["followup_applied"]
    assert "秘密题目" not in json.dumps(actual, ensure_ascii=False)
    assert "旧版本答案" not in json.dumps(actual, ensure_ascii=False)


@pytest.mark.parametrize("withdraw", ["node", "document", "cross_library_citation"])
def test_withdrawn_or_out_of_scope_citations_remove_assistant_claims_and_hints(history_db, withdraw):
    state = history_db
    turn = add_turn(state, answer="已经撤回的助教断言 [1]")
    if withdraw == "cross_library_citation":
        turn.result = {**turn.result, "citations": [{"knowledge_id": "other-library-node", "release_id": "foreign"}]}
    else:
        getattr(state, withdraw).withdrawn = True
    state.db.flush()
    actual = build(state)
    assert actual["recent_turns"][0]["question"] == turn.question
    assert actual["recent_turns"][0]["answer"] == ""
    assert actual["recent_turns"][0]["assistant_omitted"] == "invalid_or_withdrawn_citations"
    assert actual["excluded_assistant_turns"] == 1
    assert actual["retrieval_query"] == "为什么第二步成立？"
    assert "已经撤回的助教断言" not in json.dumps(actual, ensure_ascii=False)
    assert state.document.filename not in json.dumps(actual, ensure_ascii=False)


def test_long_history_is_bounded_traceable_and_does_not_modify_storage(history_db):
    state = history_db
    origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [add_turn(state, f"第 {index} 次用户提问：" + "长问题" * 500,
                     answer="冗长助教回答" * 1500,
                     created_at=origin + timedelta(minutes=index)) for index in range(70)]
    original = [(row.id, row.question, json.dumps(row.result, ensure_ascii=False)) for row in rows]
    actual = build(state, "函数连续性的定义是什么？")
    assert actual["history_count"] == 70
    assert actual["truncated"]
    assert len(actual["recent_turns"]) <= context.MAX_RECENT_TURNS
    assert 0 < len(actual["summary"]) <= context.MAX_SUMMARY_TURNS
    assert actual["recent_turns"][-1]["message_id"] == rows[-1].id
    recent_ids = {turn["message_id"] for turn in actual["recent_turns"]}
    summary_ids = {turn["message_id"] for turn in actual["summary"]}
    assert not recent_ids & summary_ids
    assert summary_ids <= {row.id for row in rows}
    assert all("answer" not in turn for turn in actual["summary"])
    assert actual["context_chars"] <= context.RECENT_CHAR_BUDGET + context.SUMMARY_CHAR_BUDGET
    assert actual["omitted_turns"] == 70 - len(recent_ids) - len(summary_ids)
    persisted = list(state.db.scalars(select(Message).order_by(Message.created_at)))
    assert [(row.id, row.question, json.dumps(row.result, ensure_ascii=False)) for row in persisted] == original


def test_no_conversation_requires_no_history_and_is_json_serializable(history_db):
    state = history_db
    actual = context.build_context(state.db, state.app, [], None, "解释这个公式")
    assert actual["history_count"] == 0
    assert actual["recent_turns"] == actual["summary"] == []
    assert actual["retrieval_query"] == "解释这个公式"
    json.dumps(actual)
