"""Exercise browser streaming states with controlled byte packets; no cloud calls."""
import json
from pathlib import Path

from dotenv import dotenv_values
from playwright.sync_api import expect, sync_playwright


def main():
    output = Path("artifacts/streaming")
    output.mkdir(parents=True, exist_ok=True)
    base = "http://127.0.0.1:8820"
    app_id = "6a09a023-5d12-4090-83fb-18a7d5ecd537"
    fixture = json.loads(Path("artifacts/no-review/live-study.json").read_text())["answer"]
    prefix, suffix = "函数连续", "不一定可导。公式 \\(\\frac{x^2}{2}+C\\) 😀 [1]。"
    fixture.update(answer=prefix+suffix, citations=fixture["citations"][:1])
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        response = page.request.post(base+"/api/auth/login", data={
            "username": "admin", "password": dotenv_values(".env")["ADMIN_PASSWORD"]},
            headers={"X-Requested-With": "calculus-studio"})
        assert response.ok
        page.add_init_script("""(() => {
          const original = window.fetch.bind(window);
          window.__streamCalls = 0;
          window.fetch = async (url, options) => {
            if (!String(url).endsWith('/chat/stream')) return original(url, options);
            window.__streamCalls++;
            const encoder = new TextEncoder();
            let controller, closed = false;
            const bytes = text => {
              const encoded = encoder.encode(text);
              // Deliberately split UTF-8 characters, CRLF separators and JSON across reads.
              for (let i = 0; i < encoded.length; i += 3) controller.enqueue(encoded.slice(i, i+3));
            };
            const event = (type, data) => bytes('event: '+type+'\\r\\ndata: '+JSON.stringify(data)+'\\r\\n\\r\\n');
            const close = () => {if (!closed) {closed = true; controller.close();}};
            const stream = new ReadableStream({start(c) {
              controller = c;
              bytes(': keep-alive\\r\\n\\r\\n');
              event('status', {message:'正在生成回答…'});
              event('delta', {text: PREFIX});
              options.signal.addEventListener('abort', () => {
                if (!closed) {closed = true; controller.error(new DOMException('Stopped','AbortError'));}
              });
              window.__finishStream = mode => {
                if (mode === 'success') {event('delta',{text:SUFFIX});event('done',RESULT);}
                if (mode === 'error') event('error',{message:'模型正文包含未声明的引用，请重试'});
                close();
              };
            }, cancel() {closed = true;}});
            return new Response(stream,{headers:{'Content-Type':'text/event-stream'}});
          };
        })();""".replace("PREFIX", json.dumps(prefix)).replace("SUFFIX", json.dumps(suffix)).replace("RESULT", json.dumps(fixture)))
        page.goto(base+"/apps/"+app_id, wait_until="networkidle")
        prompt = page.get_by_placeholder("描述你的问题，尽量包含题目条件…")
        prompt.fill("流式显示验证")
        page.get_by_role("button", name="发送问题 ↑", exact=True).click()
        expect(page.locator(".answer-text").last).to_have_text(prefix)
        expect(page.get_by_role("button", name="停止生成", exact=True)).to_be_visible()
        expect(page.locator(".citations button")).to_have_count(0)
        expect(page.get_by_role("button", name="＋ 新建对话", exact=True)).to_be_disabled()
        prompt.fill("重复提交测试")
        prompt.press("Control+Enter")
        assert page.evaluate("window.__streamCalls") == 1
        prompt.fill("")
        page.screenshot(path=str(output/"partial-answer.png"))
        page.evaluate("window.__finishStream('success')")
        expect(page.get_by_role("button", name="停止生成", exact=True)).to_have_count(0)
        expect(page.locator(".citations button")).to_have_count(1)
        expect(page.locator(".answer-text .katex")).to_have_count(1)
        assert "😀" in page.locator(".answer-text").inner_text()
        page.locator(".citations button").click()
        drawer = page.get_by_role("dialog", name="知识与来源")
        expect(drawer).to_be_visible()
        page.keyboard.press("Escape")
        expect(drawer).not_to_be_visible()
        for mode in ("cancel", "error", "disconnect"):
            page.get_by_role("button", name="＋ 新建对话", exact=True).click()
            prompt.fill("测试"+mode)
            page.get_by_role("button", name="发送问题 ↑", exact=True).click()
            expect(page.locator(".answer-text")).to_have_text(prefix)
            if mode == "cancel":
                page.get_by_role("button", name="停止生成", exact=True).click()
            else:
                page.evaluate("mode => window.__finishStream(mode)", mode)
            expect(page.get_by_role("alert")).to_be_visible()
            expect(page.locator(".answer-text")).to_have_count(0)
            expect(page.locator(".citations button")).to_have_count(0)
            expect(prompt).to_have_value("测试"+mode)
            expect(page.get_by_role("button", name="发送问题 ↑", exact=True)).to_be_enabled()
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 2")
        assert not errors, errors
        result = {"status": "passed", "mode": "controlled_stream_browser_test", "partial_visible_before_done": True,
                  "utf8_and_formula": True, "cancel_error_disconnect_clear_preview": True,
                  "duplicate_send_blocked": True, "citation_preview": True, "mobile_no_overflow": True, "errors": errors}
        (output/"browser.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False))
        browser.close()


if __name__ == "__main__":
    main()
