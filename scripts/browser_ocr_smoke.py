"""Review already imported OCR fixtures in the browser; does not submit cloud jobs."""
import json
from pathlib import Path

from dotenv import dotenv_values
from playwright.sync_api import expect, sync_playwright


def main():
    base="http://127.0.0.1:8820"
    output=Path("artifacts/ocr")
    errors=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=["--no-sandbox"])
        page=browser.new_page(viewport={"width":1440,"height":1050})
        page.on("pageerror",lambda error:errors.append(str(error)))
        response=page.request.post(base+"/api/auth/login",data={"username":"admin",
            "password":dotenv_values(".env")["ADMIN_PASSWORD"]},headers={"X-Requested-With":"calculus-studio"})
        assert response.ok
        for name in ["deployment","real-deployment"]:
            artifact=output/(name+".json")
            if not artifact.exists():
                continue
            doc=json.loads(artifact.read_text())
            page.goto(base+"/admin/knowledge-bases/"+doc["kb_id"],wait_until="networkidle")
            page.get_by_role("tab",name="原始资料").click()
            page.get_by_role("button",name="识别校对",exact=True).click()
            expect(page.get_by_text("识别结果与原图",exact=True)).to_be_visible()
            expect(page.locator(".recognition-block").first).to_be_visible()
            page.wait_for_function("Array.from(document.querySelectorAll('.recognition-image')).filter(x=>x.getBoundingClientRect().top<window.innerHeight).every(x=>x.complete&&x.naturalWidth>0)")
            assert page.locator(".katex-error").count()==0
            page.screenshot(path=str(output/("browser-"+name+".png")),full_page=True)
            if name=="deployment":
                formulas=page.locator(".recognition-block").filter(has=page.locator(".latex-source"))
                assert formulas.count()==4
                formulas.first.get_by_role("button",name="编辑对应知识").click()
                expect(page.get_by_text("知识编辑",exact=True)).to_be_visible()
                expect(page.get_by_label("公式（LaTeX）")).not_to_have_value("")
                page.wait_for_function("(()=>{const e=document.querySelector('.el-drawer');return e&&Math.abs(e.getBoundingClientRect().right-window.innerWidth)<1})()")
                page.screenshot(path=str(output/"browser-formula-editor.png"),full_page=True)
        assert not errors,errors
        browser.close()
    print("OCR 浏览器验证通过：公式渲染、原图加载、编辑对应知识和来源。")


if __name__=="__main__":
    main()
