import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.db import Base, engine
from app.models import User
from app.security import password_hash

HEADERS = {"X-Requested-With": "calculus-studio"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + str(tmp_path / "test.db"))
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-long-enough-for-encryption")
    monkeypatch.setenv("ALLOW_DEMO_PROVIDER", "true")
    monkeypatch.setenv("SEMANTICA_ENABLED", "false")
    monkeypatch.setenv("TASK_MODE", "inline")
    monkeypatch.setenv("OCR_ENABLED", "false")
    settings.cache_clear()
    engine.cache_clear()
    Base.metadata.create_all(engine())
    with Session(engine()) as db:
        db.add(User(username="admin", password_hash=password_hash("admin-test-password"), role="admin"))
        db.commit()
    from app.main import app, auth_requests
    auth_requests.clear()
    with TestClient(app, headers=HEADERS) as client:
        yield client
    engine().dispose()
    engine.cache_clear()
    settings.cache_clear()


def login(client, username="admin", password="admin-test-password"):
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text


def post(client, url, body=None):
    response = client.post("/api" + url, json=body)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def platform(client):
    login(client)
    gen = post(client, "/admin/model-profiles", {"name": "Demo", "provider": "demo", "model": "evidence", "purpose": "generation"})
    emb = post(client, "/admin/model-profiles", {"name": "Hash", "provider": "demo", "model": "hash", "purpose": "embedding", "dimension": 128})
    bundles = []
    for name in ["导数", "积分"]:
        kb = post(client, "/admin/knowledge-bases", {"name": name, "extraction_profile_id": gen["id"]})
        node = post(client, f"/admin/knowledge-bases/{kb['id']}/knowledge", {
            "kind": "formula", "title": name+"知识", "content": name+"知识的专属资料", "reviewed": True,
            "attributes": {"chapter": name, "conditions": "本例设 x 为实数"},
            "sources": [{"locator": "人工示例", "note": "测试资料", "excerpt": name+"原文"}]})
        q = post(client, f"/admin/knowledge-bases/{kb['id']}/questions", {
            "title": "求 x^2 的导数", "kind": "expression", "answer": "2*x", "reviewed": True,
            "knowledge_ids": [node["id"]], "attributes": {"difficulty": "easy"}})
        release = post(client, f"/admin/knowledge-bases/{kb['id']}/publish")
        assert release["state"] == "succeeded", release
        app = post(client, "/admin/apps", {"name": name+"应用", "model_profile_id": gen["id"],
            "embedding_profile_id": emb["id"], "kb_ids": [kb["id"]]})
        result = post(client, f"/admin/apps/{app['id']}/publish")
        assert result["state"] == "succeeded", result
        bundles.append({"kb": kb, "node": node, "question": q, "app": app})
    return {"bundles": bundles, "generation": gen, "embedding": emb}
