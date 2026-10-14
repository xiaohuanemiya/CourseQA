"""Opt-in live cloud OCR smoke check through the deployed API; credentials never printed."""
import json
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values


def main():
    root=Path(__file__).resolve().parents[1]
    fixture=root/"artifacts/ocr/scan-fixture.png"
    if not fixture.exists():
        raise SystemExit("请先运行 scripts/prepare_ocr_fixture.py 生成合成扫描样本")
    with httpx.Client(base_url="http://127.0.0.1:8820/api",timeout=90,
                      headers={"X-Requested-With":"calculus-studio"}) as client:
        response=client.post("/auth/login",json={"username":"admin","password":dotenv_values(root/".env")["ADMIN_PASSWORD"]})
        response.raise_for_status()
        kbs=client.get("/admin/knowledge-bases").json()
        kb=next((k for k in kbs if k["name"]=="OCR 验收 · 合成扫描样本"),None)
        if not kb:
            response=client.post("/admin/knowledge-bases",json={"name":"OCR 验收 · 合成扫描样本",
                "description":"明确标记的合成中文、公式、表格测试图片；保留来源，不自动发布。"})
            response.raise_for_status()
            kb=response.json()
        contents=client.get(f"/admin/knowledge-bases/{kb['id']}/contents").json()
        doc=next((d for d in contents["documents"] if d["filename"]==fixture.name and not d["withdrawn"]),None)
        if not doc:
            with fixture.open("rb") as file:
                response=client.post(f"/admin/knowledge-bases/{kb['id']}/documents",data={"mode":"scan"},
                    files={"file":(fixture.name,file,"image/png")})
            response.raise_for_status()
            doc=response.json()["document"]
            task=response.json()["task"]
            print("已提交合成图片；等待 Celery 与 PaddleOCR 完成。",flush=True)
        else:
            tasks=client.get("/admin/tasks").json()
            task=next(t for t in tasks if t["target_id"]==doc["id"])
        for _ in range(400):
            if task["state"] not in {"pending","running"}:
                break
            time.sleep(3)
            task=client.get("/admin/tasks/"+task["id"]).json()
        assert task["state"]=="succeeded",task.get("error") or task["state"]
        response=client.get(f"/admin/documents/{doc['id']}/recognition")
        response.raise_for_status()
        report=response.json()
        assert report["counts"].get("formula")==4,report["counts"]
        assert report["counts"].get("table")==1
        for block in report["blocks"]:
            assert block["asset_id"] and block["page"]==1
            asset=client.get(f"/admin/documents/{doc['id']}/assets/{block['asset_id']}")
            assert asset.status_code==200 and asset.headers["content-type"]=="image/png"
        contents=client.get(f"/admin/knowledge-bases/{kb['id']}/contents").json()
        result={"kb_id":kb["id"],"document_id":doc["id"],"task_id":task["id"],"counts":report["counts"],
            "formula_latex":[b["latex"] for b in report["blocks"] if b["label"]=="formula"],"all_pending_review":True}
        (root/"artifacts/ocr/deployment.json").write_text(json.dumps(result,ensure_ascii=False,indent=2))
        print("部署实测通过：图片上传 → 云端异步识别 → 带原图的知识；4 个公式与 1 个表格。")


if __name__=="__main__":
    main()
