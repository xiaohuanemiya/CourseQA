"""A real browser check against a running deployment. Password is read locally, never printed."""
import os
from pathlib import Path

from dotenv import dotenv_values
from playwright.sync_api import expect, sync_playwright


def main():
    base = os.environ.get("BROWSER_BASE_URL", "http://127.0.0.1:8820")
    password = dotenv_values(".env")["ADMIN_PASSWORD"]
    artifacts = Path("artifacts/browser")
    artifacts.mkdir(parents=True, exist_ok=True)
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width":1440,"height":1050},device_scale_factor=1)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base,wait_until="networkidle")
        expect(page.get_by_role("heading",name="找到适合你的学习应用")).to_be_visible()
        page.screenshot(path=str(artifacts/"home.png"), full_page=True)
        page.get_by_role("link",name="登录 / 注册").click()
        page.get_by_placeholder("字母、数字或下划线").fill("admin")
        page.get_by_placeholder("至少 8 个字符").fill(password)
        page.get_by_role("button",name="登录",exact=True).click()
        expect(page.get_by_role("link",name="应用管理",exact=True)).to_be_visible()
        page.get_by_role("button").filter(has=page.get_by_role("heading",name="极限与导数研习")).click()
        page.get_by_placeholder("描述你的问题，尽量包含题目条件…").fill("可导与连续有什么关系？")
        page.get_by_role("button",name="发送问题").click()
        expect(page.get_by_text("演示模式：以下为检索到的已发布资料",exact=False)).to_be_visible(timeout=30000)
        page.screenshot(path=str(artifacts/"study.png"),full_page=True)
        page.locator('.citations button').first.click()
        expect(page.get_by_text("来源依据",exact=True)).to_be_visible()
        page.keyboard.press("Escape")
        page.get_by_role("tab",name="自测练习").click()
        page.get_by_role("button",name="生成自测").click()
        expect(page.get_by_text("本次共 5 道题",exact=False)).to_be_visible()
        page.get_by_placeholder("输入你的答案").first.fill("0")
        page.get_by_role("button",name="提交答案").first.click()
        expect(page.get_by_text("需要复习",exact=True)).to_be_visible(timeout=15000)
        page.get_by_role("link",name="复习计划",exact=True).click()
        expect(page.get_by_role("heading",name="错题记录")).to_be_visible()
        expect(page.get_by_role("heading",name="掌握程度概览")).to_be_visible()
        page.screenshot(path=str(artifacts/"review.png"),full_page=True)
        page.get_by_role("link",name="应用管理",exact=True).click()
        page.get_by_role("button",name="编辑",exact=True).first.click()
        expect(page.get_by_text("Embedding 模型",exact=True)).to_be_visible()
        page.screenshot(path=str(artifacts/"app-settings.png"),full_page=True)
        page.get_by_role("button",name="取消",exact=True).click()
        page.get_by_role("link",name="模型与连接",exact=True).click()
        expect(page.get_by_role("heading",name="发现本地模型")).to_be_visible()
        expect(page.get_by_text("bge-m3:567m",exact=True).first).to_be_visible(timeout=20000)
        page.screenshot(path=str(artifacts/"models.png"),full_page=True)
        kbs=page.request.get(base+"/api/admin/knowledge-bases").json()
        material=next((kb for kb in kbs if kb["name"]=="高数 · 教材与课件"),None)
        if material:
            page.goto(base+"/admin/knowledge-bases/"+material["id"],wait_until="networkidle")
            expect(page.get_by_role("heading",name=material["name"],exact=True)).to_be_visible()
            page.get_by_role("tab",name="原始资料").click()
            expect(page.get_by_text("Ch1-2.pdf",exact=True).first).to_be_visible()
            page.screenshot(path=str(artifacts/"materials.png"),full_page=True)
        page.set_viewport_size({"width":390,"height":844})
        page.goto(base,wait_until="networkidle")
        page.screenshot(path=str(artifacts/"mobile.png"),full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "mobile horizontal overflow"
        assert not errors, errors
        browser.close()
    print("浏览器验证通过：登录、问答、自测判分、管理端复习计划、应用模型配置、本地模型扫描、移动端布局。")


if __name__ == "__main__":
    main()
