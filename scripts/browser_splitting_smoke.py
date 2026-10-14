"""Read-only browser check for splitting controls and real ds-flash reports."""
from pathlib import Path

from dotenv import dotenv_values
from playwright.sync_api import expect, sync_playwright


def main():
    base = "http://127.0.0.1:8820"
    output = Path("artifacts/splitting")
    output.mkdir(parents=True, exist_ok=True)
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1600, "height": 1100})
        page.on("pageerror", lambda error: errors.append(str(error)))
        response = page.request.post(base+"/api/auth/login", data={"username": "admin",
            "password": dotenv_values(".env")["ADMIN_PASSWORD"]}, headers={"X-Requested-With": "calculus-studio"})
        assert response.ok
        kbs = page.request.get(base+"/api/admin/knowledge-bases").json()
        kb = next(k for k in kbs if k["name"] == "高数 · 历年试卷与答案")
        page.goto(base+"/admin/knowledge-bases/"+kb["id"], wait_until="networkidle")
        expect(page.get_by_text("deepseek-flash", exact=False).first).to_be_visible()
        page.get_by_role("tab", name="原始资料").click()
        expect(page.get_by_text("解析与知识切分设置", exact=True)).to_be_visible()
        expect(page.get_by_text("ds-flash · deepseek-flash", exact=True).first).to_be_visible()
        page.locator(".el-select").filter(has=page.get_by_role("combobox", name="切分方法", exact=True)).click()
        for label in ["整篇文档", "按页 / 幻灯片", "按段落 / OCR 内容块", "固定字符长度", "递归文本切分", "模型语义切分"]:
            expect(page.get_by_role("option", name=label, exact=True)).to_be_visible()
        page.get_by_role("option", name="递归文本切分", exact=True).click()
        expect(page.get_by_text("重叠字符数", exact=False)).to_be_visible()
        page.locator(".el-select").filter(has=page.get_by_role("combobox", name="切分方法", exact=True)).click()
        page.get_by_role("option", name="模型语义切分", exact=True).click()
        page.screenshot(path=str(output/"settings.png"), full_page=True)
        page.get_by_role("button", name="切分报告", exact=True).first.click()
        expect(page.get_by_text("100% · 正文不改写", exact=True)).to_be_visible()
        expect(page.get_by_text("ds-flash deepseek-flash", exact=True)).to_be_visible()
        page.screenshot(path=str(output/"report.png"), full_page=True)
        page.get_by_role("dialog", name="切分报告").locator(".el-dialog__headerbtn").click()
        page.get_by_role("tab", name="知识整理").click()
        page.get_by_role("button", name="编辑", exact=True).first.click()
        expect(page.get_by_role("dialog", name="知识编辑")).to_be_visible()
        page.screenshot(path=str(output/"review.png"), full_page=True)
        assert not errors, errors
        browser.close()
    print("切分方式、模型选择、切分报告及知识编辑浏览器验证通过。")


if __name__ == "__main__":
    main()
