import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from .api import router
from .graph_api import router as graph_router
from .config import settings
from .db import engine
from .security import cipher
from .services.providers import ProviderError


@asynccontextmanager
async def lifespan(app):
    cipher()
    settings().storage_dir.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title="课程问答平台 API", version="0.1.0", lifespan=lifespan)
app.include_router(router)
app.include_router(graph_router)
auth_requests = defaultdict(deque)


@app.middleware("http")
async def request_guard(request: Request, call_next):
    if request.method in {"POST", "PUT", "DELETE", "PATCH"} and request.url.path.startswith("/api/"):
        if request.headers.get("X-Requested-With") != "calculus-studio":
            return JSONResponse({"detail": "缺少请求校验标识"}, status_code=403)
    if request.url.path in {"/api/auth/login", "/api/auth/register"}:
        key = request.client.host
        now = time.monotonic()
        attempts = auth_requests[key]
        while attempts and now-attempts[0] > 60:
            attempts.popleft()
        if len(attempts) >= 20:
            return JSONResponse({"detail": "请求过于频繁，请稍后重试"}, status_code=429)
        attempts.append(now)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(ProviderError)
async def provider_error(request, exc):
    return JSONResponse({"detail": str(exc), "status": "provider_unavailable"}, status_code=503)


@app.exception_handler(IntegrityError)
async def integrity_error(request, exc):
    return JSONResponse({"detail": "数据冲突，请刷新后重试"}, status_code=409)


@app.get("/api/health")
def health():
    with engine().connect() as connection:
        connection.execute(text("SELECT 1"))
    return {"status": "ok", "demo_enabled": settings().allow_demo_provider,
            "semantica_enabled": settings().semantica_enabled}
