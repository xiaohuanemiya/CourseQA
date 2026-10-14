#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 scripts/bootstrap_env.py
for name in calculus-db calculus-redis calculus-api calculus-worker calculus-web; do
  if podman container exists "$name"; then
    echo "容器 $name 已存在。更新代码使用 scripts/podman-rebuild.sh；未修改现有容器。" >&2
    exit 1
  fi
done
podman build --network host -f deploy/Containerfile.api -t localhost/calculus-api:0.1 .
podman build --network host -f deploy/Containerfile.web -t localhost/calculus-web:0.1 .
podman volume create calculus-postgres >/dev/null
podman volume create calculus-data >/dev/null
podman volume create calculus-redis >/dev/null
podman run -d --name calculus-db --network host --restart unless-stopped --env-file .env \
  -v calculus-postgres:/var/lib/postgresql/data docker.io/pgvector/pgvector:pg16 \
  postgres -p 15433 -c listen_addresses=127.0.0.1
podman run -d --name calculus-redis --network host --restart unless-stopped \
  -v calculus-redis:/data docker.m.daocloud.io/library/redis:7.4-alpine \
  redis-server --port 16380 --bind 127.0.0.1 --appendonly yes
ready=false
for _ in $(seq 1 30); do
  if podman exec calculus-db pg_isready -p 15433 -U calculus -d calculus >/dev/null 2>&1; then ready=true; break; fi
  sleep 1
done
if [[ "$ready" != true ]]; then echo '数据库未就绪，请查看 podman logs calculus-db' >&2; exit 1; fi
podman run --rm --network host --env-file .env -v calculus-data:/data localhost/calculus-api:0.1 alembic upgrade head
podman run --rm --network host --env-file .env -v calculus-data:/data localhost/calculus-api:0.1 python -m app.cli demo
podman run -d --name calculus-api --network host --restart unless-stopped --env-file .env \
  -v calculus-data:/data localhost/calculus-api:0.1
podman run -d --name calculus-worker --network host --restart unless-stopped --env-file .env \
  -v calculus-data:/data localhost/calculus-api:0.1 celery -A app.tasks.celery worker --pool=prefork --concurrency="${WORKER_CONCURRENCY:-4}" --loglevel=INFO
podman run -d --name calculus-web --network host --restart unless-stopped localhost/calculus-web:0.1
echo '已启动：http://127.0.0.1:8820；账号 admin，密码见 .env 的 ADMIN_PASSWORD。'
