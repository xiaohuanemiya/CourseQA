#!/usr/bin/env bash
set -euo pipefail
podman stop calculus-web calculus-api calculus-worker calculus-redis calculus-db
echo '项目容器已停止，容器和数据卷均保留。恢复可运行 podman start calculus-db calculus-redis calculus-api calculus-worker calculus-web。'
