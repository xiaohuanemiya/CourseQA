"""Upload the user's originals into review-only KBs through the public admin API."""
import argparse
import json
from pathlib import Path

import httpx
from dotenv import dotenv_values

parser=argparse.ArgumentParser()
parser.add_argument("directory",type=Path)
parser.add_argument("--base-url",default="http://127.0.0.1:8820")
parser.add_argument("--raw-only",action="store_true",help="Explicitly import original blocks without claiming LLM extraction")
parser.add_argument("--extraction-profile-id")
parser.add_argument("--parse-mode", choices=["text","auto","scan"], default="auto",
    help="PDF/images always use configured PaddleOCR; Office/text files use local parsing")
parser.add_argument("--split-method", choices=["document","page","paragraph","fixed","recursive","semantic"],default="semantic")
parser.add_argument("--chunk-size",type=int,default=1200)
args=parser.parse_args()
credentials=dotenv_values(".env")
with httpx.Client(base_url=args.base_url,timeout=180,headers={"X-Requested-With":"calculus-studio"}) as client:
    response=client.post("/api/auth/login",json={"username":"admin","password":credentials["ADMIN_PASSWORD"]})
    response.raise_for_status()
    profiles=client.get("/api/admin/model-profiles").json()
    profile_id=args.extraction_profile_id
    if args.raw_only:
        profile_id=next(p["id"] for p in profiles if p["provider"]=="demo" and p["purpose"]=="generation")
    if not profile_id:
        raise SystemExit("请指定真实抽取模型，或显式使用 --raw-only 进行原文导入")
    existing=client.get("/api/admin/knowledge-bases").json()
    kbs={}
    for category in ["教材与课件","历年试卷与答案"]:
        name="高数 · "+category
        kb=next((k for k in existing if k["name"]==name),None)
        if not kb:
            response=client.post("/api/admin/knowledge-bases",json={"name":name,
                "description":"用户提供的真实高数资料。保留原文与来源，可编辑后直接发布。",
                "extraction_profile_id":profile_id})
            response.raise_for_status()
            kb=response.json()
        elif not args.raw_only:
            response=client.put("/api/admin/knowledge-bases/"+kb["id"],json={"name":kb["name"],"description":kb["description"],"extraction_profile_id":profile_id})
            response.raise_for_status()
        kbs[category]=kb
    tasks=[]
    for path in sorted(args.directory.iterdir()):
        if path.suffix.lower() not in {".pdf",".docx",".pptx",".md",".txt"}:
            continue
        category="历年试卷与答案" if path.name.startswith(("2020-","2021-","2022-")) else "教材与课件"
        with path.open("rb") as file:
            response=client.post(f"/api/admin/knowledge-bases/{kbs[category]['id']}/documents",
                data={"mode":args.parse_mode,"splitting":json.dumps({"method":"paragraph" if args.raw_only else args.split_method,"model_profile_id":profile_id,"chunk_size":args.chunk_size})},files={"file":(path.name,file)})
        if response.status_code==409:
            print("已存在，跳过：",path.name)
            continue
        response.raise_for_status()
        task=response.json()["task"]
        tasks.append(task["id"])
        print("已提交导入：",path.name,"任务",task["id"])
    print(f"共提交 {len(tasks)} 个任务；完成后可直接发布。")
