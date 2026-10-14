from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app.db import engine
from app.models import Document
from app.services import learning, retrieval
from .conftest import post
from .test_document_retrieval import PAPER, document, fetch, publish


def test_explicit_comparison_resolves_each_year_independently():
    documents = {str(i): SimpleNamespace(id=str(i), filename=name, version=1) for i, name in enumerate([
        PAPER, PAPER.replace(".pdf", "答案.pdf"), "2022-2023一元B上月考.pdf"])}
    result = retrieval.document_plan("比较2021-2022一元B上月考和2022-2023一元B上月考的考点", documents)
    assert result["status"] == "comparison"
    assert len(result["comparison_groups"]) == 2
    assert {doc["id"] for doc in result["documents"]} == set(documents)


@pytest.mark.parametrize("question,status", [
    ("比较《微积分基础讲义》和《微积分进阶讲义》", "comparison"),
    ("比较《微积分基础讲义》、《微积分进阶讲义》和《微积分专题讲义》", "comparison"),
    ("比较《微积分基础讲义》和《不存在的讲义》", "not_found"),
    ("比较《微积分基础讲义》、《微积分进阶讲义》、《微积分专题讲义》和《第四本讲义》", "ambiguous"),
    ("比较《微积分基础讲义》和《微积分基础讲义》", "ambiguous"),
    ("函数连续与可导的区别", "general"),
])
def test_comparison_does_not_guess_missing_or_excessive_targets(question, status):
    documents = {str(i): SimpleNamespace(id=str(i), filename=name + ".pdf", version=1) for i, name in enumerate([
        "微积分基础讲义", "微积分进阶讲义", "微积分专题讲义"])}
    assert retrieval.document_plan(question, documents)["status"] == status


def test_comparison_overview_allocates_each_source_its_own_budget(client, platform, monkeypatch):
    bundle = platform["bundles"][0]
    first, first_ids = document(bundle, PAPER, [(f"题型 {i}", "甲卷考点正文" * 550) for i in range(20)])
    second, second_ids = document(bundle, "2022-2023一元B上月考.pdf",
                                  [(f"专题 {i}", "乙卷考点正文" * 1100) for i in range(2)])
    publish(client, bundle)
    monkeypatch.setattr(retrieval, "embed", lambda *args: pytest.fail("Overview does not need embeddings"))
    result = fetch(bundle, "比较2021-2022一元B上月考和2022-2023一元B上月考的考点")
    assert result.context["status"] == "comparison" and result.context["strategy"] == "comparison_overview"
    assert {first, second} == {doc["id"] for node in result for doc in node["documents"]}
    assert any(node["id"] in first_ids for node in result) and any(node["id"] in second_ids for node in result)
    for doc_id in (first, second):
        assert sum(len(node["content"]) for node in result if node["documents"][0]["id"] == doc_id) <= 12000
    assert sum(len(node["content"]) for node in result) <= retrieval.MAX_CONTEXT_CHARACTERS
    assert len(result.context["comparison_coverage"]) == 2
    assert result.context["coverage"]["complete"] is False


def test_comparison_search_and_tool_search_keep_both_sources_and_original_boundary(client, platform):
    bundle, foreign = platform["bundles"]
    first, first_ids = document(bundle, "微积分基础讲义.pdf", [(f"链式法则 {i}", "链式法则用于复合函数求导") for i in range(15)])
    second, second_ids = document(bundle, "微积分进阶讲义.pdf", [("链式法则", "链式法则还适用于高阶复合函数")])
    outside, _ = document(bundle, "微积分其他讲义.pdf", [("链式法则", "不允许引入的其他资料")])
    document(foreign, "微积分基础讲义.pdf", [("链式法则", "另一应用的秘密")])
    publish(client, bundle)
    publish(client, foreign)
    question = "比较《微积分基础讲义》和《微积分进阶讲义》的链式法则"
    result = fetch(bundle, question)
    assert result.context["strategy"] == "comparison_search"
    assert set(first_ids) & {node["id"] for node in result}
    assert set(second_ids) & {node["id"] for node in result}
    assert sum(node["id"] in first_ids for node in result) <= 4
    with Session(engine()) as db:
        app, releases = learning.scope(db, bundle["app"]["id"])
        supplemented = retrieval.retrieve(db, app, releases, "链式法则", allowed_document_ids={first, second})
        rejected = retrieval.retrieve(db, app, releases, "比较《微积分基础讲义》和《微积分其他讲义》",
                                      allowed_document_ids={first, second})
    assert supplemented.context["status"] == "comparison"
    assert {first, second} == {doc["id"] for node in supplemented for doc in node["documents"]}
    assert outside not in {doc["id"] for node in supplemented for doc in node["documents"]}
    assert not rejected and rejected.context["status"] == "not_found"


def test_missing_or_withdrawn_comparison_target_does_not_silently_compare_partial_set(client, platform):
    bundle = platform["bundles"][0]
    document(bundle, "微积分基础讲义.pdf", [("基础", "有效的基础正文")])
    second, _ = document(bundle, "微积分进阶讲义.pdf", [("进阶", "即将撤回的进阶正文")])
    publish(client, bundle)
    with Session(engine()) as db:
        db.get(Document, second).withdrawn = True
        db.commit()
    result = fetch(bundle, "比较《微积分基础讲义》和《微积分进阶讲义》")
    assert not result and result.context["status"] == "not_found"
    assert "进阶" in result.context["message"] and "尚未开始比较" in result.context["message"]


def test_followup_specific_question_does_not_inherit_overview_intent(client, platform):
    bundle = platform["bundles"][0]
    source, ids = document(bundle, PAPER, [(f"第 {i} 题", f"第 {i} 题考查函数求导与链式法则") for i in range(1, 12)])
    document(bundle, "2022-2023一元B上月考.pdf", [("第二题", "另一年份不得混入")])
    publish(client, bundle)
    first = post(client, f"/apps/{bundle['app']['id']}/chat", {"message": f"总结{PAPER}的考点"})
    second = post(client, f"/apps/{bundle['app']['id']}/chat", {
        "message": "第二题怎么做？", "conversation_id": first["conversation_id"]})
    assert first["retrieval"]["strategy"] == "document_overview"
    assert second["retrieval"]["strategy"] == "document_search"
    assert second["retrieval"]["inherited_scope"] is True
    assert second["retrieval"]["conversation_context"]["followup_applied"]
    assert {doc["id"] for cite in second["citations"] for doc in cite["documents"]} == {source}
    assert {cite["knowledge_id"] for cite in second["citations"]} <= set(ids)


def test_new_explicit_target_overrides_inherited_reference(client, platform):
    bundle = platform["bundles"][0]
    first, _ = document(bundle, "微积分基础讲义.pdf", [("链式法则", "原讲义内容")])
    second, second_ids = document(bundle, "微积分进阶讲义.pdf", [("链式法则", "新讲义内容")])
    publish(client, bundle)
    with Session(engine()) as db:
        app, releases = learning.scope(db, bundle["app"]["id"])
        result = retrieval.retrieve(db, app, releases, "解释《微积分进阶讲义》的链式法则",
                                    inherited_document_ids=[first], topic_hint="总结微积分基础讲义")
    assert [node["id"] for node in result] == second_ids
    assert result.context["documents"][0]["id"] == second


def test_comparison_answer_preference_is_applied_per_source_family(client, platform):
    bundle = platform["bundles"][0]
    document(bundle, PAPER, [("第一题", "第一年试卷正文")])
    _, first_answers = document(bundle, PAPER.replace(".pdf", "答案.pdf"), [("第一题解析", "第一年答案解析正文")])
    _, second_paper = document(bundle, "2022-2023一元B上月考.pdf", [("第二题", "第二年仅有试卷正文")])
    publish(client, bundle)
    result = fetch(bundle, "比较2021-2022一元B上月考和2022-2023一元B上月考的答案考点")
    assert {node["id"] for node in result} == set(first_answers + second_paper)
    assert all(group["selected_units"] for group in result.context["comparison_coverage"])


def test_inherited_comparison_cannot_silently_drop_an_unavailable_source(client, platform):
    bundle = platform["bundles"][0]
    first, _ = document(bundle, "微积分基础讲义.pdf", [("链式法则", "链式法则正文")])
    second, _ = document(bundle, "微积分进阶讲义.pdf", [("链式法则", "进阶求导正文")])
    publish(client, bundle)
    with Session(engine()) as db:
        db.get(Document, second).withdrawn = True
        db.commit()
        app, releases = learning.scope(db, bundle["app"]["id"])
        for scope in ({"inherited_document_ids": [first, second]}, {"allowed_document_ids": {first, second}}):
            result = retrieval.retrieve(db, app, releases, "它们有什么区别", **scope)
            assert not result and result.context["status"] == "not_found"
            assert "部分资料已不可用" in result.context["message"]
