import io
import pytest

from app.models import ReleaseVector
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import engine
from .conftest import login, post


@pytest.mark.parametrize("answer,ids,expected_status", [
    ("资料依据 [1]", ["1"], 200),
    ("资料依据 [999]", ["1"], 503),
    ("资料依据 [999]", ["999"], 503),
    ("无引用的回答", [], 503),
])
def test_generated_citations_are_checked_before_saving(client, platform, monkeypatch, answer, ids, expected_status):
    from app.models import ModelProfile
    with Session(engine()) as db:
        db.get(ModelProfile, platform["generation"]["id"]).provider="openai"
        db.commit()
    monkeypatch.setattr("app.services.learning.generate", lambda *args, **kwargs: {
        "status":"answered", "answer":answer, "citation_ids":ids})
    app_id=platform["bundles"][0]["app"]["id"]
    response=client.post(f"/api/apps/{app_id}/chat",json={"message":"导数知识"})
    assert response.status_code==expected_status, response.text
    history=client.get(f"/api/apps/{app_id}/conversations").json()
    assert len(history)==(1 if expected_status==200 else 0)


def student(client, name="student"):
    post(client, "/auth/register", {"username": name, "password": "student-test-password"})


def test_public_catalog_and_admin_authorization(client, platform):
    post(client, "/auth/logout")
    assert len(client.get("/api/apps").json()) == 2
    assert client.get("/api/admin/knowledge-bases").status_code == 401
    student(client)
    assert client.get("/api/admin/knowledge-bases").status_code == 403
    assert client.post("/api/admin/apps", json={}).status_code == 403


def test_scope_is_enforced_for_every_read_path(client, platform):
    a, b = platform["bundles"]
    student(client)
    app_id = a["app"]["id"]
    assert client.get(f"/api/apps/{app_id}/sources/{b['node']['id']}").status_code == 404
    assert client.get(f"/api/apps/{app_id}/graph").status_code == 403
    options = client.get(f"/api/apps/{app_id}/study-options").json()
    assert {n["id"] for n in options["knowledge"]} == {a["node"]["id"]}
    result = post(client, f"/apps/{app_id}/chat", {"message": "导数知识"})
    assert result["citations"]
    assert {c["knowledge_id"] for c in result["citations"]} == {a["node"]["id"]}
    assert client.post(f"/api/apps/{app_id}/chat", json={"message":"hello", "kb_ids":[b["kb"]["id"]]}).status_code == 422
    quiz = post(client, f"/apps/{app_id}/quizzes", {"count":20})
    assert quiz["count"] == 1
    assert "answer" not in quiz["questions"][0]
    assert {q["id"] for q in quiz["questions"]} == {a["question"]["id"]}
    bad = client.post(f"/api/apps/{app_id}/quizzes", json={"knowledge_ids":[b["node"]["id"]]})
    assert bad.status_code == 404


def test_conversation_quiz_and_records_are_private(client, platform):
    app_id = platform["bundles"][0]["app"]["id"]
    student(client, "alice")
    result = post(client, f"/apps/{app_id}/chat", {"message":"导数知识"})
    quiz = post(client, f"/apps/{app_id}/quizzes", {})
    student(client, "bob")
    assert client.get(f"/api/apps/{app_id}/conversations/{result['conversation_id']}").status_code == 404
    assert client.post(f"/api/apps/{app_id}/chat", json={"message":"导数", "conversation_id":result["conversation_id"]}).status_code == 404
    assert client.post(f"/api/apps/{app_id}/quizzes/{quiz['id']}/answers", json={"question_id":quiz["questions"][0]["id"],"answer":"2*x"}).status_code == 404
    assert client.get(f"/api/apps/{app_id}/review-plan").status_code == 403
    assert client.get(f"/api/admin/apps/{app_id}/review-plan").status_code == 403


def test_math_grading_and_review_records(client, platform):
    a = platform["bundles"][0]
    student(client)
    learner_id = client.get("/api/auth/me").json()["id"]
    quiz = post(client, f"/apps/{a['app']['id']}/quizzes", {})
    url = f"/apps/{a['app']['id']}/quizzes/{quiz['id']}/answers"
    result = post(client, url, {"question_id":a["question"]["id"],"answer":"3*x"})
    assert result["verdict"] == "incorrect"
    duplicate = post(client, url, {"question_id":a["question"]["id"],"answer":"2*x"})
    assert duplicate == result
    assert client.get(f"/api/apps/{a['app']['id']}/review-plan").status_code == 403
    login(client)
    review = client.get(f"/api/admin/apps/{a['app']['id']}/review-plan", params={"user_id": learner_id}).json()
    assert review["total_attempts"] == 1
    assert len(review["mistakes"]) == 1
    assert review["items"][0]["mastery"] == 1/3


def test_edit_is_draft_until_atomic_publish_and_withdraw_immediate(client, platform):
    a = platform["bundles"][0]
    base = f"/admin/knowledge-bases/{a['kb']['id']}"
    node = a["node"]
    body = {k:node[k] for k in ["kind","title","content","attributes","sources","reviewed"]}
    body["content"] = "全新修订的导数内容"
    assert client.put("/api"+base+"/knowledge/"+node["id"], json=body).status_code == 200
    old = client.get(f"/api/apps/{a['app']['id']}/sources/{node['id']}").json()
    assert old["content"] == node["content"]
    result = post(client, base+"/publish")
    assert result["state"] == "succeeded", result
    new = client.get(f"/api/apps/{a['app']['id']}/sources/{node['id']}").json()
    assert new["content"] == body["content"]
    assert new["release_id"] != old["release_id"]
    assert client.get(f"/api/apps/{a['app']['id']}/sources/{node['id']}", params={"release_id":old["release_id"]}).status_code == 409
    client.delete("/api"+base+"/knowledge/"+node["id"])
    assert client.get(f"/api/apps/{a['app']['id']}/sources/{node['id']}").status_code == 404
    quiz = post(client, f"/apps/{a['app']['id']}/quizzes", {})
    assert quiz["count"] == 0


def test_failed_build_preserves_current_release(client, platform, monkeypatch):
    a = platform["bundles"][0]
    old = client.get(f"/api/apps/{a['app']['id']}/sources/{a['node']['id']}").json()["release_id"]
    def fail(*args, **kwargs):
        raise ValueError("Embedding 故障")
    monkeypatch.setattr("app.services.knowledge.embed", fail)
    task = post(client, f"/admin/knowledge-bases/{a['kb']['id']}/publish")
    assert task["state"] == "failed"
    assert client.get(f"/api/apps/{a['app']['id']}/sources/{a['node']['id']}").json()["release_id"] == old


def test_prerequisite_cycle_blocks_publication(client, platform):
    a = platform["bundles"][0]
    base = f"/admin/knowledge-bases/{a['kb']['id']}"
    node = post(client, base+"/knowledge", {"kind":"concept","title":"另一个知识","content":"内容", "reviewed":True,
        "sources":[{"locator":"手工","note":"依据"}]})
    for x,y in [(node["id"],a["node"]["id"]),(a["node"]["id"],node["id"])]:
        post(client, base+"/relations", {"source_id":x,"target_id":y,"kind":"prerequisite","reviewed":True})
    task = post(client, base+"/publish")
    assert task["state"] == "failed"
    assert "循环" in task["error"]


def test_formula_metadata_does_not_require_approval(client, platform):
    a=platform["bundles"][0]
    base=f"/admin/knowledge-bases/{a['kb']['id']}"
    post(client,base+"/knowledge",{"kind":"formula","title":"条件缺失的公式","content":"x^n 求导",
        "reviewed":True,"sources":[{"locator":"人工输入","note":"测试缺失条件"}]})
    task=post(client,base+"/publish")
    assert task["state"]=="succeeded", task


def test_application_embedding_change_builds_separate_index(client, platform):
    a = platform["bundles"][0]
    profile = post(client, "/admin/model-profiles", {"name":"Second","purpose":"embedding","provider":"demo","model":"hash2","dimension":64})
    app = a["app"]
    body = {**app["draft"],"embedding_profile_id":profile["id"]}
    assert client.put(f"/api/admin/apps/{app['id']}",json=body).status_code == 200
    result = post(client, f"/admin/apps/{app['id']}/publish")
    assert result["state"] == "succeeded", result
    chat = post(client, f"/apps/{app['id']}/chat", {"message":"导数知识"})
    assert chat["citations"]
    with Session(engine()) as db:
        vectors = db.scalars(select(ReleaseVector).where(ReleaseVector.knowledge_id==a["node"]["id"])).all()
        assert {len(v.embedding) for v in vectors} == {128,64}


def test_upload_preserves_provenance_and_access_boundary(client, platform):
    a,b = platform["bundles"]
    result = client.post(f"/api/admin/knowledge-bases/{a['kb']['id']}/documents",
        files={"file":("notes.md", io.BytesIO("# 手工笔记\n导数公式需审核。".encode()), "text/markdown")})
    assert result.status_code == 200, result.text
    doc = result.json()["document"]
    assert result.json()["task"]["state"] == "succeeded", result.text
    contents = client.get(f"/api/admin/knowledge-bases/{a['kb']['id']}/contents").json()
    node = next(n for n in contents["knowledge"] if n["sources"][0].get("document_id") == doc["id"])
    assert not node["reviewed"]
    assert "章节" in node["sources"][0]["locator"]
    assert post(client, f"/admin/knowledge-bases/{a['kb']['id']}/publish")["state"] == "succeeded"
    student(client)
    assert client.get(f"/api/apps/{b['app']['id']}/documents/{doc['id']}").status_code == 404


def test_secret_never_returned_and_csrf_header_required(client):
    login(client)
    item = post(client, "/admin/model-profiles", {"name":"Cloud","provider":"openai","model":"example", "base_url":"https://example.org/v1","api_key":"private-key"})
    assert "private-key" not in str(item)
    assert "secret" not in item and item["has_api_key"]
    assert "private-key" not in client.get("/api/admin/model-profiles").text
    client.headers.pop("X-Requested-With")
    assert client.post("/api/auth/logout").status_code == 403
