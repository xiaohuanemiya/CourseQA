from sqlalchemy.orm import Session

from app.db import engine
from app.models import Application
from .conftest import login, post


def test_retired_features_hidden_in_catalog_admin_editor_and_legacy_input(client, platform):
    bundle = platform["bundles"][0]
    app_id = bundle["app"]["id"]
    with Session(engine()) as db:
        app = db.get(Application, app_id)
        app.draft = {**app.draft, "features": ["chat", "review"]}
        app.published = {**app.published, "features": ["chat", "review"]}
        db.commit()
    assert client.get(f"/api/apps/{app_id}").json()["features"] == ["chat"]
    assert all("review" not in a["features"] for a in client.get("/api/apps").json())
    app = next(a for a in client.get("/api/admin/apps").json() if a["id"] == app_id)
    assert app["draft"]["features"] == app["published"]["features"] == ["chat"]
    updated = client.put(f"/api/admin/apps/{app_id}", json={**app["draft"], "features": ["chat", "review"]})
    assert updated.status_code == 200 and updated.json()["draft"]["features"] == ["chat"]
    assert client.put(f"/api/admin/apps/{app_id}", json={**app["draft"], "features": ["review"]}).status_code == 422
    options = client.get(f"/api/apps/{app_id}/study-options").json()
    assert options == {"suggestions": [bundle["node"]["title"]], "knowledge": [], "chapters": []}
    assert client.get(f"/api/admin/apps/{app_id}/review-plan").status_code == 200


def test_admin_only_views_preserve_app_and_learner_boundaries(client, platform):
    a, b = platform["bundles"]
    app_id = a["app"]["id"]
    post(client, "/auth/register", {"username": "learner-a", "password": "student-test-password"})
    learner_id = client.get("/api/auth/me").json()["id"]
    post(client, f"/apps/{app_id}/chat", {"message": "导数知识"})
    paths = [f"/apps/{app_id}/graph", f"/apps/{app_id}/review-plan", f"/admin/apps/{app_id}/graph",
             f"/admin/apps/{app_id}/review-plan", f"/admin/apps/{app_id}/learners", "/admin/graph"]
    for path in paths:
        assert client.get("/api"+path).status_code == 403
    options = client.get(f"/api/apps/{app_id}/study-options").json()
    assert set(options) == {"suggestions", "knowledge", "chapters"}
    assert options["knowledge"] == [{"id": a["node"]["id"], "title": a["node"]["title"]}]
    assert options["chapters"] == ["导数"]
    post(client, "/auth/logout")
    for path in paths+[f"/apps/{app_id}/study-options"]:
        assert client.get("/api"+path).status_code == 401
    login(client)
    for path in paths:
        assert client.get("/api"+path).status_code == 200
    learners = client.get(f"/api/admin/apps/{app_id}/learners").json()
    assert learner_id in {u["id"] for u in learners}
    assert all(set(u) == {"id", "username"} for u in learners)
    assert learner_id not in {u["id"] for u in client.get(f"/api/admin/apps/{b['app']['id']}/learners").json()}
    graph = client.get(f"/api/admin/apps/{app_id}/graph").json()
    assert {n["id"] for n in graph["nodes"]} == {a["node"]["id"]}
    plan = client.get(f"/api/admin/apps/{app_id}/review-plan", params={"user_id": learner_id}).json()
    assert plan["learner"]["id"] == learner_id and plan["total_attempts"] == 0
    assert {n["id"] for n in plan["items"]} == {a["node"]["id"]}
    assert client.get(f"/api/admin/apps/{b['app']['id']}/review-plan", params={"user_id": learner_id}).status_code == 404
