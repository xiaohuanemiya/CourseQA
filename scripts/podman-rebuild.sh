#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
podman build --network host -f deploy/Containerfile.api -t localhost/calculus-api:0.1 .
podman build --network host -f deploy/Containerfile.web -t localhost/calculus-web:0.1 .
# Only this project's replaceable service containers are replaced. Data volumes are retained.
podman stop calculus-web calculus-api calculus-worker
podman rm calculus-web calculus-api calculus-worker
podman run --rm --network host --env-file .env -v calculus-data:/data localhost/calculus-api:0.1 alembic upgrade head
podman run -d --name calculus-api --network host --restart unless-stopped --env-file .env -v calculus-data:/data localhost/calculus-api:0.1
podman run -d --name calculus-worker --network host --restart unless-stopped --env-file .env -v calculus-data:/data localhost/calculus-api:0.1 celery -A app.tasks.celery worker --pool=prefork --concurrency="${WORKER_CONCURRENCY:-4}" --loglevel=INFO
podman run -d --name calculus-web --network host --restart unless-stopped localhost/calculus-web:0.1
