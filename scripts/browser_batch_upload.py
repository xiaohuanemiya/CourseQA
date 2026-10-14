"""Verify real batch imports plus isolated browser failure/retry cases; no cloud calls."""
import json
import os
import re
from collections import Counter
from pathlib import Path

from dotenv import dotenv_values
from playwright.sync_api import expect, sync_playwright


def main():
    base = os.environ.get("BROWSER_BASE_URL", "http://127.0.0.1:8820")
    output = Path("artifacts/batch-upload")
    output.mkdir(parents=True, exist_ok=True)
    report, errors, requests, held = {}, [], [], []
    complete_slow = False
    progress_calls = Counter()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1440, "height": 1050})
        page.on("pageerror", lambda error: errors.append(str(error)))
        headers = {"X-Requested-With": "calculus-studio"}
        response = page.request.post(base+"/api/auth/login", headers=headers, data={
            "username": "admin", "password": dotenv_values(".env")["ADMIN_PASSWORD"]})
        assert response.ok
        response = page.request.post(base+"/api/admin/knowledge-bases", headers=headers,
                                     data={"name": "批量上传验收（临时）", "description": "浏览器验收，结束后删除"})
        assert response.ok
        kb_id = response.json()["id"]
        upload_url = base+f"/api/admin/knowledge-bases/{kb_id}/documents"

        def task(task_id, state, progress=0, error=None):
            return {"id": task_id, "state": state, "progress": progress, "error": error}

        def intercept_upload(route):
            data = route.request.post_data_buffer
            name = re.search(br'filename="([^"]+)"', data).group(1).decode()
            options = json.loads(re.search(br'name="splitting"\r\n\r\n([^\r]+)', data).group(1))
            assert options["method"] == "paragraph", "Never call a model during this browser check"
            requests.append({"filename": name, "splitting": options})
            attempts = sum(r["filename"] == name for r in requests)
            if name == "upload-retry.txt":
                if attempts == 1:
                    held.append(route)
                    return
                route.fulfill(json={"document": {"id": "mock-upload"}, "task": task("mock-upload", "succeeded", 100)})
            elif name == "processing-retry.txt":
                route.fulfill(json={"document": {"id": "mock-process"},
                                    "task": task("mock-process-1", "failed", 20, "模拟解析失败，可重试")})
            elif name == "slow.txt":
                route.fulfill(json={"document": {"id": "mock-slow"}, "task": task("mock-slow", "pending")})
            else:
                route.continue_()

        def intercept_task(route):
            task_id = route.request.url.rsplit("/", 1)[-1]
            if route.request.method == "POST":
                assert route.request.url.endswith("mock-process-1/retry")
                progress_calls["retry"] += 1
                route.fulfill(json=task("mock-process-2", "pending"))
            else:
                progress_calls[task_id] += 1
                done = complete_slow if task_id == "mock-slow" else progress_calls[task_id] >= 2
                route.fulfill(json=task(task_id, "succeeded" if done else "running", 100 if done else 55))

        def choose_method(label):
            page.locator('.el-select').filter(has=page.get_by_role("combobox", name="切分方法", exact=True)).click()
            page.get_by_role("option", name=label, exact=True).click()

        def row(name):
            return page.locator(".upload-row").filter(has=page.get_by_text(name, exact=True))

        def file(name, content):
            return {"name": name, "mimeType": "text/plain", "buffer": content}

        try:
            page.route(upload_url, intercept_upload)
            page.route(re.compile(r"/api/admin/tasks/mock-[^/]+(?:/retry)?$"), intercept_task)
            page.goto(base+f"/admin/knowledge-bases/{kb_id}?tab=documents", wait_until="networkidle")
            expect(page.get_by_role("button", name="选择文件（可多选）", exact=True)).to_be_visible()
            input_file = page.locator('input[type="file"]')
            expect(input_file).to_have_attribute("multiple", "")
            choose_method("按段落 / OCR 内容块")
            input_file.set_input_files([
                file("课程笔记一.txt", "# 定义\n学习课程中的基本概念。".encode()),
                file("课程笔记二.md", "# 方法\n通过例题理解课程方法。".encode()),
                file("不支持.exe", b"invalid"), file("空文件.txt", b""),
                file("超限.txt", b"x" * (30*1024*1024+1)),
            ])
            expect(page.locator('.upload-row[data-state="succeeded"]')).to_have_count(2, timeout=30000)
            expect(page.locator('.upload-row[data-state="invalid"]')).to_have_count(3)
            assert len(requests) == 2
            contents = page.request.get(base+f"/api/admin/knowledge-bases/{kb_id}/contents").json()
            assert len(contents["documents"]) == 2 and all(d["state"] == "ready" for d in contents["documents"])
            assert all(d["parse_options"]["splitting"]["method"] == "paragraph" for d in contents["documents"])
            input_file.set_input_files(file("重复内容.txt", "# 定义\n学习课程中的基本概念。".encode()))
            expect(row("重复内容.txt")).to_have_attribute("data-state", "skipped", timeout=10000)
            assert len(page.request.get(base+f"/api/admin/knowledge-bases/{kb_id}/contents").json()["documents"]) == 2
            report["real_imports"] = 2
            report["invalid_files_isolated"] = 3
            report["duplicate_not_reimported"] = True

            # The first upload is held while settings change; the original batch must keep its settings.
            input_file.set_input_files([file("upload-retry.txt", b"retry upload"),
                                       file("processing-retry.txt", b"retry processing"), file("slow.txt", b"long task")])
            expect(row("upload-retry.txt")).to_have_attribute("data-state", "uploading")
            choose_method("递归文本切分")
            assert held
            held.pop().fulfill(status=503, json={"detail": "模拟上传失败"})
            expect(row("upload-retry.txt")).to_have_attribute("data-state", "upload_failed")
            expect(row("processing-retry.txt")).to_have_attribute("data-state", "failed")
            expect(row("slow.txt")).to_have_attribute("data-state", re.compile("pending|running"))
            row("upload-retry.txt").get_by_role("button", name="重试上传", exact=True).click()
            expect(row("upload-retry.txt")).to_have_attribute("data-state", "succeeded")
            row("processing-retry.txt").get_by_role("button", name="重试处理", exact=True).click()
            expect(row("processing-retry.txt")).to_have_attribute("data-state", "succeeded", timeout=15000)
            assert sum(r["filename"] == "processing-retry.txt" for r in requests) == 1
            assert progress_calls["retry"] == 1
            report["separate_upload_and_processing_retries"] = True
            report["batch_settings_preserved"] = True

            page.get_by_role("tab", name="知识整理", exact=True).click()
            page.get_by_role("tab", name="原始资料", exact=True).click()
            page.reload(wait_until="networkidle")
            expect(row("slow.txt")).to_have_attribute("data-state", re.compile("pending|running"))
            assert sum(r["filename"] == "slow.txt" for r in requests) == 1
            complete_slow = True
            expect(row("slow.txt")).to_have_attribute("data-state", "succeeded", timeout=15000)
            expect(page.locator('.upload-row[data-state="succeeded"]')).to_have_count(5)
            report["task_tracking_restored_after_reload"] = True
            page.screenshot(path=str(output/"desktop.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "mobile overflow"
            page.screenshot(path=str(output/"mobile.png"), full_page=True)
            report["mobile_layout"] = True
            assert not errors, errors
            report["page_errors"] = errors
            report["cloud_calls"] = 0
        finally:
            for pending_route in held:
                pending_route.abort()
            response = page.request.delete(base+"/api/admin/knowledge-bases/"+kb_id, headers=headers)
            assert response.ok
            report["temporary_kb_deleted"] = True
            (output/"browser.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n")
            browser.close()
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
