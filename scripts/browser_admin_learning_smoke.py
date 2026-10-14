"""Verify management-only learning views with an isolated temporary student account."""
import json
import secrets
import subprocess
from pathlib import Path

from dotenv import dotenv_values
from playwright.sync_api import expect, sync_playwright


def main():
    base = "http://127.0.0.1:8820"
    app_id = "6a09a023-5d12-4090-83fb-18a7d5ecd537"
    output = Path("artifacts/admin-only-learning")
    output.mkdir(parents=True, exist_ok=True)
    headers = {"X-Requested-With": "calculus-studio"}
    username, student_id = "ui_learning_"+secrets.token_hex(6), None
    report = {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            admin_page = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            admin_page.on("pageerror", lambda error: errors.append(str(error)))
            response = admin_page.request.post(base+"/api/auth/login", headers=headers,
                data={"username": "admin", "password": dotenv_values(".env")["ADMIN_PASSWORD"]})
            assert response.ok
            public = admin_page.request.get(base+f"/api/apps/{app_id}").json()
            assert public["features"] == ["chat"]
            plan = admin_page.request.get(base+f"/api/admin/apps/{app_id}/review-plan").json()
            assert len(plan["items"]) == 1685
            admin_page.goto(base+"/admin/review-plans?app_id="+app_id, wait_until="networkidle")
            expect(admin_page.get_by_role("heading", name="复习计划", exact=True)).to_be_visible()
            expect(admin_page.get_by_role("heading", name="admin 的复习计划", exact=True)).to_be_visible()
            expect(admin_page.get_by_role("button", name="查看知识", exact=True)).to_have_count(15)
            admin_page.get_by_role("button", name="查看知识", exact=True).first.click()
            drawer = admin_page.get_by_role("dialog", name="知识与来源")
            expect(drawer).to_be_visible()
            expect(drawer.get_by_text("来源依据", exact=True)).to_be_visible()
            admin_page.keyboard.press("Escape")
            expect(drawer).not_to_be_visible()
            admin_page.screenshot(path=str(output/"admin-review-plans.png"), full_page=True)
            admin_page.set_viewport_size({"width": 390, "height": 844})
            assert admin_page.evaluate("document.documentElement.scrollWidth <= innerWidth+2")
            admin_page.set_viewport_size({"width": 1440, "height": 1000})
            admin_page.get_by_role("link", name="图数据库", exact=True).click()
            expect(admin_page.get_by_role("heading", name="图数据库", exact=True)).to_be_visible()
            admin_page.goto(base+"/admin/apps", wait_until="networkidle")
            admin_page.locator(".app-card").filter(has=admin_page.get_by_role("heading", name="高数复习", exact=True)).get_by_role("button", name="编辑", exact=True).click()
            drawer = admin_page.get_by_role("dialog", name="编辑应用配置")
            expect(drawer.get_by_role("checkbox", name="复习计划", exact=True)).to_have_count(0)
            expect(drawer.get_by_role("checkbox", name="知识问答", exact=True)).to_be_checked()
            expect(drawer.get_by_role("checkbox", name="自测练习", exact=True)).to_have_count(1)
            student_context = browser.new_context(viewport={"width": 1440, "height": 1000})
            student_page = student_context.new_page()
            student_page.on("pageerror", lambda error: errors.append(str(error)))
            created = student_page.request.post(base+"/api/auth/register", headers=headers,
                data={"username": username, "password": secrets.token_urlsafe(24)})
            assert created.ok
            student_id = created.json()["id"]
            calls = []
            student_page.on("request", lambda request: calls.append(request.url))
            student_page.goto(base+"/apps/"+app_id, wait_until="networkidle")
            expect(student_page.get_by_role("heading", name="高数复习", exact=True)).to_be_visible()
            expect(student_page.get_by_role("tab", name="知识问答", exact=True)).to_be_visible()
            expect(student_page.get_by_role("tab", name="复习计划", exact=True)).to_have_count(0)
            expect(student_page.get_by_role("tab", name="知识地图", exact=True)).to_have_count(0)
            expect(student_page.get_by_role("link", name="复习计划", exact=True)).to_have_count(0)
            assert not any(url.endswith(("/graph", "/review-plan")) for url in calls)
            assert any(url.endswith("/study-options") for url in calls)
            paths = [f"/apps/{app_id}/graph", f"/apps/{app_id}/review-plan", "/admin/graph",
                     f"/admin/apps/{app_id}/graph", f"/admin/apps/{app_id}/review-plan", f"/admin/apps/{app_id}/learners"]
            for path in paths:
                assert student_page.request.get(base+"/api"+path).status == 403
            student_page.screenshot(path=str(output/"student-app.png"), full_page=True)
            student_page.set_viewport_size({"width": 390, "height": 844})
            assert student_page.evaluate("document.documentElement.scrollWidth <= innerWidth+2")
            student_page.goto(base+"/admin/review-plans", wait_until="networkidle")
            expect(student_page).to_have_url(base+"/")
            expect(student_page.get_by_role("button", name="复习计划", exact=True)).to_have_count(0)
            assert not errors, errors
            report = {"status": "passed", "public_features": public["features"], "admin_plan_items": len(plan["items"]),
                      "student_tabs_removed": True, "no_student_graph_or_plan_fetch": True,
                      "student_forbidden_endpoints": len(paths), "admin_sources_and_graph": True,
                      "app_editor_student_features_only": True, "mobile_no_overflow": True, "page_errors": errors}
            browser.close()
    finally:
        if student_id:
            cleanup = """
import sys
from sqlalchemy import select,delete
from sqlalchemy.orm import Session
from app.db import engine
from app.models import User,LoginSession,Attempt,Quiz,Conversation
with Session(engine()) as db:
    user=db.get(User,sys.argv[1])
    assert user and user.username==sys.argv[2] and user.username.startswith('ui_learning_') and user.role=='student'
    for model in (Attempt,Quiz,Conversation):
        assert db.scalar(select(model.id).where(model.user_id==user.id)) is None
    db.execute(delete(LoginSession).where(LoginSession.user_id==user.id))
    db.delete(user)
    db.commit()
"""
            subprocess.run(["podman", "exec", "-i", "calculus-api", "python", "-", student_id, username],
                           input=cleanup, text=True, check=True, capture_output=True)
            report["temporary_student_deleted"] = True
        (output/"browser.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
