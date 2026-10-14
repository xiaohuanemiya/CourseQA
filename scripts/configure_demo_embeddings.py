"""Use discovered real embeddings in the two explicitly synthetic demonstration apps."""
import time

import httpx
from dotenv import dotenv_values

with httpx.Client(base_url="http://127.0.0.1:8820",timeout=180,
                  headers={"X-Requested-With":"calculus-studio"}) as client:
    response=client.post("/api/auth/login",json={"username":"admin","password":dotenv_values('.env')['ADMIN_PASSWORD']})
    response.raise_for_status()
    profiles=client.get("/api/admin/model-profiles").json()
    for app in client.get("/api/admin/apps").json():
        draft=app["draft"]
        generation=next(p for p in profiles if p["id"]==draft["model_profile_id"])
        if generation["provider"]!="demo" or draft["name"] not in {"极限与导数研习","积分专项研习"}:
            continue
        name="bge-m3:567m" if draft["name"]=="极限与导数研习" else "nomic-embed-text:v1.5"
        embedding=next(p for p in profiles if p["provider"]=="ollama" and p["model"]==name)
        if app.get("published",{}).get("embedding_profile_id")!=embedding["id"]:
            response=client.put(f"/api/admin/apps/{app['id']}",json={**draft,"embedding_profile_id":embedding["id"]})
            response.raise_for_status()
            response=client.post(f"/api/admin/apps/{app['id']}/publish")
            response.raise_for_status()
            task=response.json()
            for _ in range(150):
                if task["state"] not in {"pending","running"}:
                    break
                time.sleep(1)
                task=client.get(f"/api/admin/tasks/{task['id']}").json()
            assert task["state"]=="succeeded",task.get("error","发布未完成")
        result=client.post(f"/api/apps/{app['id']}/chat",json={"message":"可导与连续有什么关系" if "导数" in draft["name"] else "不定积分与原函数"})
        result.raise_for_status()
        assert result.json()["citations"],"真实向量检索没有返回引用"
        print(draft["name"],"已发布并验证实际 pgvector 检索：",name,embedding["dimension"],"维")
