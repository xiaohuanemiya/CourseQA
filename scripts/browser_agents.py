"""Exercise agent settings, native tools, child agents and SSE through a real browser.

Uses a temporary database and a local HTTP model fixture; no deployed data or cloud calls.
Build the frontend first: npm --prefix frontend run build.
"""
import asyncio
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import uvicorn
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix="agents-browser-") as temp:
        os.environ.update(DATABASE_URL="sqlite:///" + temp + "/test.db", STORAGE_DIR=temp,
            SECRET_KEY="local-browser-test-secret-not-a-real-credential", ALLOW_DEMO_PROVIDER="true",
            SEMANTICA_ENABLED="false", TASK_MODE="inline", OCR_ENABLED="false", COOKIE_SECURE="false")
        sys.path.insert(0, str(ROOT / "backend"))
        from app.db import Base, engine
        from app.main import app
        from app.models import User
        from app.security import password_hash
        from fastapi import Request
        from fastapi.responses import FileResponse, StreamingResponse
        from fastapi.staticfiles import StaticFiles
        from sqlalchemy.orm import Session
        Base.metadata.create_all(engine())
        with Session(engine()) as db:
            db.add(User(username="admin", password_hash=password_hash("test-browser-password"), role="admin"))
            db.commit()
        control = {"tools": 0, "children": 0, "streamed": 0, "slow": False}

        @app.post("/test-model/chat/completions")
        async def fake_model(request: Request):
            body = await request.json()
            assert body["model"] == "local-fixture" and "max_tokens" not in body
            messages = body["messages"]
            content, calls = "OK", []
            if body.get("tools"):
                control["tools"] += 1
                if control["slow"]:
                    await asyncio.sleep(2)
                task = json.loads(messages[1]["content"])["task"]
                if task == "核查导数":
                    assert "spawn_agent" not in {t["function"]["name"] for t in body["tools"]}
                    if len(messages) == 2:
                        control["children"] += 1
                        name, args = "calculate", {"operation": "differentiate", "expression": "x^2"}
                    else:
                        name, args = None, None
                        content = "计算工具结果为 2*x，课程依据 [1]。"
                elif len(messages) == 2:
                    name, args = "read_skill", {"name": "calculus-verification"}
                elif len(messages) == 4:
                    name, args = "spawn_agent", {"name": "求导核验", "task": "核查导数"}
                else:
                    name, args = None, None
                    content = "可以结合计算结果与课程证据解释 [1]。"
                if name:
                    calls = [{"id": f"call-{len(messages)}", "type": "function", "function": {
                        "name": name, "arguments": json.dumps(args)}}]
            if body.get("stream"):
                if not calls and json.loads(messages[1]["content"])["task"] != "核查导数":
                    assert any('2*x' in m["content"] for m in messages if m["role"] == "user")
                    control["streamed"] += 1
                    content = json.dumps({"status": "answered", "answer": "根据课程资料，导数是变化率 [1]。"
                                          "数学工具算得 \\(2x\\)。", "citation_ids": ["1"],
                                          "supports": [{"claim": "导数是变化率", "citation_id": "1",
                                                        "quote": "导数是函数在一点的瞬时变化率"}]}, ensure_ascii=False)

                async def frames():
                    if calls:
                        tool = calls[0]
                        args = tool["function"]["arguments"]
                        for i in range(0, len(args), 6):
                            chunk = {"index": 0, "function": {"arguments": args[i:i+6]}}
                            if i == 0:
                                chunk.update(id=tool["id"], type="function")
                                chunk["function"]["name"] = tool["function"]["name"]
                            yield "data: " + json.dumps({"choices": [{"delta": {"tool_calls": [chunk]},
                                                       "finish_reason": None}]}) + "\n\n"
                    else:
                        for i in range(0, len(content), 6):
                            yield "data: " + json.dumps({"choices": [{"delta": {"content": content[i:i+6]},
                                                       "finish_reason": None}]}) + "\n\n"
                            await asyncio.sleep(.01)
                    yield "data: " + json.dumps({"choices": [{"delta": {},
                        "finish_reason": "tool_calls" if calls else "stop"}]}) + "\n\n"
                    yield 'data: {"choices":[],"usage":{"prompt_tokens":100,"completion_tokens":20}}\n\n'
                    yield "data: [DONE]\n\n"
                return StreamingResponse(frames(), media_type="text/event-stream")
            return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "OK"}}]}

        app.mount("/assets", StaticFiles(directory=ROOT / "frontend/dist/assets"), name="test-assets")

        @app.get("/{path:path}")
        def frontend(path: str):
            return FileResponse(ROOT / "frontend/dist/index.html")

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        base = f"http://127.0.0.1:{sock.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(.05)
        assert server.started
        output = ROOT / "artifacts/agents"
        output.mkdir(parents=True, exist_ok=True)
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
                page = browser.new_page(viewport={"width": 1440, "height": 1080})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                headers = {"X-Requested-With": "calculus-studio"}

                def post(path, data=None):
                    response = page.request.post(base + "/api" + path, headers=headers, data=data)
                    assert response.ok, response.text()
                    return response.json()

                post("/auth/login", {"username": "admin", "password": "test-browser-password"})
                model = post("/admin/model-profiles", {"name": "Agent 测试模型", "model": "local-fixture",
                    "provider": "openai", "base_url": base + "/test-model", "purpose": "generation"})
                emb = post("/admin/model-profiles", {"name": "Hash", "model": "hash", "provider": "demo",
                    "purpose": "embedding", "dimension": 128})
                kb = post("/admin/knowledge-bases", {"name": "微积分课程"})
                post(f"/admin/knowledge-bases/{kb['id']}/knowledge", {"kind": "concept", "title": "导数知识",
                    "content": "导数是函数在一点的瞬时变化率，x 的平方的导数为 2x。",
                    "sources": [{"locator": "测试讲义第 1 页", "excerpt": "导数是瞬时变化率。", "note": "本地验收资料"}]})
                assert post(f"/admin/knowledge-bases/{kb['id']}/publish")["state"] == "succeeded"
                application = post("/admin/apps", {"name": "自主助教验收", "model_profile_id": model["id"],
                    "embedding_profile_id": emb["id"], "kb_ids": [kb["id"]], "features": ["chat"]})
                app_id = application["id"]
                page.goto(base + "/admin/apps", wait_until="networkidle")
                page.get_by_role("button", name="编辑", exact=True).click()
                drawer = page.get_by_role("dialog", name="编辑应用配置")
                switch = drawer.get_by_role("switch")
                expect(switch).to_have_attribute("aria-checked", "false")
                drawer.locator(".el-switch").click()
                expect(drawer.locator(".el-select__tags-text").filter(has_text="符号数学方法")).to_be_visible()
                drawer.locator("summary").click()
                expect(drawer.get_by_text("最多子 agent 数量（0 表示关闭拆分）")).to_be_visible()
                page.screenshot(path=str(output / "settings.png"), full_page=True)
                drawer.get_by_role("button", name="保存草稿", exact=True).click()
                expect(drawer).not_to_be_visible()
                page.get_by_role("button", name="发布", exact=True).click()
                expect(page.get_by_text("应用已发布", exact=True)).to_be_visible(timeout=15000)
                page.goto(base + "/apps/" + app_id, wait_until="networkidle")
                prompt = page.get_by_placeholder("描述你的问题，尽量包含题目条件…")
                recovered_reads = []
                page.on("request", lambda req: recovered_reads.append(req.url)
                        if "/chat/requests/" in req.url and req.method == "GET" else None)

                def drop_completion(route):
                    response = route.fetch()
                    raw = response.text()
                    boundary = raw.index("event: done")
                    # The server commits successfully, but the browser loses its final frame.
                    route.fulfill(response=response, body=raw[:boundary])

                page.route("**/chat/stream", drop_completion)
                prompt.fill("解释导数知识，并验证 x^2 的导数")
                page.get_by_role("button", name="发送问题 ↑", exact=True).click()
                expect(page.locator(".answer-text")).to_contain_text("导数是变化率", timeout=15000)
                expect(page.get_by_role("button", name="停止生成", exact=True)).to_have_count(0, timeout=10000)
                page.unroute("**/chat/stream", drop_completion)
                assert recovered_reads, "Network recovery did not query the existing request"
                page.locator(".answer-supports summary").click()
                expect(page.locator(".answer-supports blockquote")).to_contain_text("瞬时变化率")
                completed = page.request.get(base + f"/api/apps/{app_id}/conversations").json()
                saved = page.request.get(base + f"/api/apps/{app_id}/conversations/{completed[0]['id']}").json()[0]["result"]
                calls_before_replay = control["tools"]
                replay = post(f"/apps/{app_id}/chat", {"message": "解释导数知识，并验证 x^2 的导数",
                                                     "request_id": saved["request_id"]})
                assert replay == saved and control["tools"] == calls_before_replay
                page.locator(".agent-trace summary").click()
                expect(page.locator(".agent-trace")).to_contain_text("calculus-verification")
                expect(page.locator(".agent-trace .metrics")).to_contain_text("模型调用 5 次")
                expect(page.locator(".agent-trace .metrics")).to_contain_text("500 输入 / 100 输出")
                expect(page.locator(".agent-trace")).to_contain_text("子任务完成：求导核验")
                expect(page.locator(".citations button")).to_have_count(1)
                expect(page.locator(".math-result .katex")).to_have_count(1)
                page.screenshot(path=str(output / "desktop.png"), full_page=True)
                page.reload(wait_until="networkidle")
                page.locator(".conversation-link").first.click()
                expect(page.locator(".agent-trace summary")).to_contain_text("1 个子任务")
                page.locator(".agent-trace summary").click()
                expect(page.locator(".agent-trace")).to_contain_text("求导核验")
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                page.screenshot(path=str(output / "mobile.png"), full_page=True)
                # Abort while the real backend is awaiting a native model tool round.
                page.set_viewport_size({"width": 1440, "height": 1080})
                control["slow"] = True
                before = control["tools"]
                page.get_by_role("button", name="＋ 新建对话", exact=True).click()
                prompt.fill("取消导数知识子任务")
                page.get_by_role("button", name="发送问题 ↑", exact=True).click()
                expect(page.locator(".stream-status")).to_contain_text("分析问题", timeout=5000)
                page.get_by_role("button", name="停止生成", exact=True).click()
                expect(page.get_by_role("alert")).to_contain_text("已停止生成")
                page.wait_for_timeout(2500)
                assert control["tools"] <= before + 1
                history = page.request.get(base + f"/api/apps/{app_id}/conversations").json()
                assert len(history) == 1
                assert not errors, errors
                report = {"settings_published": True, "native_tool_round_trip": True, "child_agents": control["children"],
                    "streamed_answers": control["streamed"], "trace_restored": True, "cancellation_no_save": True,
                    "mobile_layout": True, "network_recovered_saved_result": bool(recovered_reads),
                    "duplicate_no_extra_calls": True, "evidence_quotes": True, "page_errors": errors, "cloud_calls": 0}
                (output / "browser.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
                print(json.dumps(report, ensure_ascii=False))
                browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            sock.close()
            engine().dispose()


if __name__ == "__main__":
    main()
