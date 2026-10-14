import httpx
from dotenv import dotenv_values

with httpx.Client(base_url="http://127.0.0.1:8820",timeout=120,
                  headers={"X-Requested-With":"calculus-studio"}) as client:
    response=client.post("/api/auth/login",json={"username":"admin","password":dotenv_values('.env')['ADMIN_PASSWORD']})
    response.raise_for_status()
    base=client.get("/api/admin/environment").json()["local_model_url"]
    discovered=client.get("/api/admin/models/discover",params={"base_url":base})
    discovered.raise_for_status()
    profiles=client.get("/api/admin/model-profiles").json()
    for model in discovered.json():
        if not model["embedding"]:
            continue
        if any(p["model"]==model["name"] and p["base_url"]==base for p in profiles):
            continue
        result=client.post("/api/admin/model-profiles",json={"name":"本地 · "+model["name"],"provider":"ollama",
            "purpose":"embedding","model":model["name"],"base_url":base,"dimension":model["dimension"]})
        result.raise_for_status()
        tested=client.post(f"/api/admin/model-profiles/{result.json()['id']}/test")
        tested.raise_for_status()
        print("已添加并实测本地向量模型：",model["name"],model["dimension"],"维")
