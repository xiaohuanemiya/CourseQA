import json
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app.db import engine
from app.models import Document, Knowledge, Relation
from app.schemas import ChatInput
from app.services import learning, retrieval
from .conftest import post

QUERY = "告诉我2021-2022一元B上月考考了什么\n"
PAPER = "2021-2022一元B上月考.pdf"


def document(bundle, filename, entries):
    with Session(engine()) as db:
        doc = Document(kb_id=bundle["kb"]["id"], filename=filename, path="/unused/test.pdf",
                       checksum="test", state="ready")
        db.add(doc)
        db.flush()
        nodes = []
        for i, (title, content) in enumerate(entries):
            node = Knowledge(kb_id=doc.kb_id, kind="note", title=title, content=content,
                attributes={"chapter": f"第 {i+1} 页 · 内容块 1", "splitting": {"chunk_index": i}},
                sources=[{"document_id": doc.id, "page": i+1, "locator": f"第 {i+1} 页", "excerpt": content}])
            db.add(node)
            nodes.append(node)
        db.commit()
        return doc.id, [n.id for n in nodes]


def publish(client, bundle):
    result = post(client, f"/admin/knowledge-bases/{bundle['kb']['id']}/publish")
    assert result["state"] == "succeeded", result


def fetch(bundle, message=QUERY, use_graph=True):
    with Session(engine()) as db:
        app, releases = learning.scope(db, bundle["app"]["id"])
        return learning.retrieve(db, app, releases, message, use_graph)


def test_exam_overview_covers_body_instead_of_other_years_headers(client, platform, monkeypatch):
    bundle = platform["bundles"][0]
    entries = [("试卷标题与考生信息", "2021-2022 学年一元微积分(B上)月考试卷 姓名___")]
    entries += [(f"题型{i}", f"第{i}类题目：考查知识点{i}，共{i}分。") for i in range(1, 13)]
    entries += [("页码", "12")]
    doc_id, ids = document(bundle, PAPER, entries)
    document(bundle, PAPER.replace(".pdf", "答案.pdf"), [("答案正文", "参考答案内容")])
    # Header near-matches outnumber the old top-20 candidate budget.
    for year in range(1990, 2021):
        document(bundle, f"{year}-{year+1}一元B上月考.pdf", [("试卷标题", f"{year}-{year+1}月考")])
    document(bundle, PAPER.replace("月考", "A卷"), [("另一份试卷", "不能混入A卷内容")])
    publish(client, bundle)
    # An overview resolved by document membership must not depend on an embedding call.
    monkeypatch.setattr(retrieval, "embed", lambda *a: pytest.fail("Unnecessary embedding request"))
    evidence = fetch(bundle)
    assert {n["id"] for n in evidence} == set(ids[1:-1])
    assert all(n["documents"][0]["id"] == doc_id for n in evidence)
    assert evidence.context["strategy"] == "document_overview"
    assert evidence.context["coverage"] == {"available_units": 12, "selected_units": 12, "complete": True}
    system, user = learning.generation_prompt(SimpleNamespace(published={"prompt": "课程助教"}),
                                             ChatInput(message=QUERY), evidence)
    prompt = json.loads(user)
    assert len(prompt["evidence"]) == 12 and "覆盖不完整" in system
    assert prompt["evidence"][0]["documents"][0]["filename"] == PAPER
    assert prompt["evidence"][0]["documents"][0]["pages"]
    # Both public endpoints use the same evidence, preserving citations and streaming.
    result = post(client, f"/apps/{bundle['app']['id']}/chat", {"message": QUERY})
    assert len(result["citations"]) == 12
    stream = client.post(f"/api/apps/{bundle['app']['id']}/chat/stream", json={"message": QUERY})
    assert stream.status_code == 200
    final = next(json.loads(frame.splitlines()[1][6:]) for frame in stream.text.split("\n\n")
                 if frame.startswith("event: done\n"))
    assert final["retrieval"]["coverage"]["complete"] is True
    assert {c["knowledge_id"] for c in final["citations"]} == set(ids[1:-1])


def test_document_lookup_respects_scope_drafts_withdrawals_and_published_text(client, platform):
    a, b = platform["bundles"]
    doc_id, ids = document(a, PAPER, [("题目", "已发布正文")])
    document(b, PAPER, [("题目", "其他应用的秘密正文")])
    publish(client, a)
    publish(client, b)
    document(a, PAPER, [("题目", "未发布草稿正文")])
    with Session(engine()) as db:
        db.get(Knowledge, ids[0]).content = "尚未发布的新修订"
        db.commit()
    evidence = fetch(a)
    assert [n["content"] for n in evidence] == ["已发布正文"]
    with Session(engine()) as db:
        db.get(Knowledge, ids[0]).withdrawn = True
        db.commit()
    assert not fetch(a)
    with Session(engine()) as db:
        db.get(Knowledge, ids[0]).withdrawn = False
        db.get(Document, doc_id).withdrawn = True
        db.commit()
    result = post(client, f"/apps/{a['app']['id']}/chat", {"message": QUERY})
    assert result["status"] == "insufficient_evidence"
    assert result["citations"] == [] and result["retrieval"]["documents"] == []
    assert "未在当前应用" in result["answer"]


def test_specific_document_search_cannot_expand_into_another_paper(client, platform):
    a = platform["bundles"][0]
    _, ids = document(a, PAPER, [("连续与可导", "连续与可导的区别：可导必连续，连续不一定可导。")])
    _, others = document(a, "2022-2023一元B上月考.pdf", [("连续与可导", "另一年独有的内容")])
    with Session(engine()) as db:
        db.add(Relation(kb_id=a["kb"]["id"], source_id=ids[0], target_id=others[0], kind="related"))
        db.commit()
    publish(client, a)
    evidence = fetch(a, "解释2021-2022一元B上月考中连续与可导的区别")
    assert [n["id"] for n in evidence] == ids
    assert evidence.context["strategy"] == "document_search"


def test_general_concept_search_downranks_boilerplate(client, platform):
    a = platform["bundles"][0]
    _, ids = document(a, "函数讲义.pdf", [("试卷标题", "可导与连续"),
        ("可导与连续的关系", "函数可导必连续，连续不一定可导。"), ("页码", "1")])
    publish(client, a)
    evidence = fetch(a, "可导与连续的关系")
    assert evidence[0]["id"] == ids[1]
    assert evidence.context["strategy"] == "hybrid"


def test_pure_vector_evaluation_bypasses_document_resolution(client, platform, monkeypatch):
    a = platform["bundles"][0]
    document(a, PAPER, [("填空题", "数列与函数极限的计算")])
    publish(client, a)
    monkeypatch.setattr(retrieval, "document_plan", lambda *a: pytest.fail("Pure vector must not resolve documents"))
    monkeypatch.setattr(retrieval, "lexical_ranking", lambda *a: pytest.fail("Pure vector must not use BM25"))
    with Session(engine()) as db:
        app, releases = learning.scope(db, a["app"]["id"])
        evidence = retrieval.retrieve(db, app, releases, "数列与函数极限", vector_only=True)
    assert evidence and evidence.context["strategy"] == "vector"
    assert len(evidence) <= 8


@pytest.mark.parametrize("question,status", [
    ("告诉我2024-2025一元B上月考考了什么", "not_found"),
    ("2021-2022一元A上月考考了什么", "not_found"),
    ("2021-2022线性代数月考考了什么", "not_found"),
    ("2021-2022一元B上期末考了什么", "not_found"),
    ("总结2021—2022一元微积分（B 上）月考", "matched"),
    ("月考考了什么", "ambiguous"),
    ('请解释"continuity"的含义', "general"),
    ("总结《不存在的课程讲义》", "not_found"),
])
def test_document_resolution_does_not_guess_year_course_or_exam(question, status):
    names = [PAPER, PAPER.replace(".pdf", "答案.pdf"), "2022-2023一元B上月考.pdf"]
    docs = {str(i): SimpleNamespace(id=str(i), filename=name, version=1) for i, name in enumerate(names)}
    assert retrieval.document_plan(question, docs)["status"] == status


def test_answer_overview_and_header_only_document(client, platform):
    a = platform["bundles"][0]
    document(a, PAPER, [("试卷标题", "2021-2022月考试卷")])
    _, ids = document(a, PAPER.replace(".pdf", "答案.pdf"), [("解答题", "递推数列题解答，用单调有界定理求极限。")])
    publish(client, a)
    assert [n["id"] for n in fetch(a, "总结2021-2022一元B上月考答案")] == ids
    with Session(engine()) as db:
        db.get(Knowledge, ids[0]).withdrawn = True
        db.commit()
    result = post(client, f"/apps/{a['app']['id']}/chat", {"message": QUERY})
    assert result["citations"] == [] and "只有标题" in result["answer"]


def test_long_document_overview_distributes_pages_and_reports_partial_coverage():
    nodes = {str(i): {"id": str(i), "title": f"第{i}章", "kind": "note", "content": f"章节{i}正文" * 10,
        "attributes": {"chapter": f"第{i}章", "splitting": {"chunk_index": i}},
        "documents": [{"id": "doc", "pages": [i]}]} for i in range(1, 81)}
    result = retrieval.bounded_evidence(retrieval.overview_order(nodes), {}, retrieval.MAX_OVERVIEW_UNITS, 80)
    assert len(result) == 32 and {"1", "80"} <= {n["id"] for n in result}
    assert result.context["coverage"]["complete"] is False
    assert sum(len(n["content"]) for n in result) <= retrieval.MAX_CONTEXT_CHARACTERS
    large = {key: {**n, "content": n["content"] * 30} for key, n in nodes.items()}
    result = retrieval.bounded_evidence(retrieval.overview_order(large), {}, 32, 80)
    assert {"1", "80"} <= {n["id"] for n in result}
    assert sum(len(n["content"]) for n in result) <= retrieval.MAX_CONTEXT_CHARACTERS
    huge = {**nodes["1"], "content": "大" * 30000}
    result = retrieval.bounded_evidence([huge], {}, 32, 1)
    assert result[0]["excerpt_truncated"] and not result.context["coverage"]["complete"]
