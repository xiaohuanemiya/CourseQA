"""Create local-only deployment credentials once. Never print secrets."""
import os
from pathlib import Path
import secrets

path = Path(".env")
if path.exists():
    print(".env 已存在，未覆盖")
else:
    password = secrets.token_hex(20)
    content = f"""DATABASE_URL=postgresql+psycopg://calculus:{password}@127.0.0.1:15433/calculus
POSTGRES_USER=calculus
POSTGRES_DB=calculus
POSTGRES_PASSWORD={password}
SECRET_KEY={secrets.token_hex(32)}
ADMIN_PASSWORD={secrets.token_urlsafe(18)}
STORAGE_DIR=/data
TASK_MODE=celery
REDIS_URL=redis://127.0.0.1:16380/0
SEMANTICA_ENABLED=true
ALLOW_DEMO_PROVIDER=true
EMBEDDING_BASE_URL=http://127.0.0.1:11434
MODEL_TIMEOUT=90
COOKIE_SECURE=false
"""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write(content)
    print("已生成 .env（权限 0600）；管理员初始密码见 ADMIN_PASSWORD。仅启用明确标注的演示应用。")
