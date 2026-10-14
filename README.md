# CourseQA · 课程问答平台

CourseQA 将课程资料整理为可发布的知识库，为学生提供带来源的问答和自测。管理员可以按课程创建应用，分别选择生成模型与 Embedding 模型，管理资料版本、知识关系和学习记录。

项目支持 OpenAI 兼容接口与 Ollama，内置高数人工示例用于体验流程。演示模式只展示检索证据；真实问答、模型语义切分和自主 Agent 需要自行配置模型。

## 主要功能

- **课程资料管理**：上传 PDF、图片、DOCX、PPTX、TXT、Markdown，选择切分方式，编辑内容并发布；保留原文、页码和版本归属。
- **带依据的问答**：混合检索、知识图扩展、多轮追问，以及明确指定的 2～3 份资料对比；校验正文引用，并展示模型提供的结论与原文摘录。
- **自主 Agent 与 Tool Use**：模型判断直接回答、调用工具或创建独立子 agent；支持有限并行、执行预算、取消、工具结果复用和执行记录。
- **五个内置 Skills**：课程证据检索、微积分核验、资料比较、SymPy 方法、科学证据分析；按需读取，随应用发布固定版本。
- **可靠的流式交互**：SSE 增量回答、请求编号去重、断线后查询已保存结果；按用户与模型配置控制问答并发。
- **自测与管理端分析**：受限 SymPy 计算与表达式判分、错题和复习建议、知识图谱浏览及关系生成。
- **应用和用户隔离**：按应用绑定知识库，草稿与发布分离；撤回立即影响检索，会话和作答记录按用户隔离。

原文摘录匹配不等于结论在语义上正确。测试验证软件行为；真实模型准确率、引用支持率和学习效果需另行评测。

## 技术栈

| 层次 | 技术 |
|---|---|
| 前端 | Vue 3、TypeScript、Vite、Element Plus、ECharts、KaTeX |
| 后端 | Python、FastAPI、SQLAlchemy、Alembic |
| 数据与任务 | PostgreSQL / pgvector、Redis、Celery |
| 检索与计算 | Semantica 0.7.0、NetworkX、SymPy |
| 模型与识别 | OpenAI 兼容接口、Ollama、PaddleOCR-VL-1.6 云端 OCR |
| 部署 | Linux Podman、Nginx |

## 快速启动：Linux Podman

需要 Linux、Bash、Python 3 和可正常运行的 Podman。脚本使用 host 网络；首次运行需要联网拉取镜像和依赖，宿主机无需先安装 Node 或 Python 项目依赖。请确保下表端口可用。

```bash
git clone https://github.com/xiaohuanemiya/CourseQA.git
cd CourseQA
bash scripts/podman-up.sh
```

首次启动会自动生成权限为 `0600` 的 `.env`，创建数据库、执行迁移，并初始化两个演示应用。管理员账号为 `admin`，随机密码保存在本地 `.env` 的 `ADMIN_PASSWORD`。已有 `.env` 不会被覆盖；首次体验建议由脚本生成，不要直接将含占位值的 `.env.example` 当作部署配置。

| 服务 | 地址 | 容器 |
|---|---|---|
| 页面 | [http://127.0.0.1:8820](http://127.0.0.1:8820) | `calculus-web` |
| API / OpenAPI | [http://127.0.0.1:8821/docs](http://127.0.0.1:8821/docs) | `calculus-api` |
| PostgreSQL | `127.0.0.1:15433` | `calculus-db` |
| Redis | `127.0.0.1:16380` | `calculus-redis` |
| 后台任务 | 无公开端口 | `calculus-worker` |

服务默认只监听本机回环地址。数据保存在 `calculus-postgres`、`calculus-data`、`calculus-redis` 卷；镜像和容器继续使用 `calculus-*` 名称。对外提供服务时应配置 HTTPS 反向代理和安全 Cookie。端口调整需同时核对部署脚本、环境配置和 [Nginx 配置](deploy/nginx.conf)。

### 更新、停止和恢复

拉取更新后重建服务；脚本保留数据卷，并在新版服务启动前执行 `alembic upgrade head`。

```bash
git pull --ff-only
bash scripts/podman-rebuild.sh

# 停止，保留容器和数据卷
bash scripts/podman-down.sh

# 恢复已停止的容器
podman start calculus-db calculus-redis calculus-api calculus-worker calculus-web

# 查看后台任务日志
podman logs calculus-worker
```

`podman-up.sh` 遇到同名容器会停止并提示，不覆盖现有部署。升级前请备份数据库和文件卷；自行部署时也必须执行数据库迁移，不能只替换代码。现有迁移包括知识关系来源、请求记录和跨进程并发租约表。

## 配置真实模型与课程资料

1. 登录管理端，在“模型与连接”中分别添加**生成模型**与 **Embedding 模型**。Ollama 可扫描配置地址；云端填写完整 OpenAI 兼容 API 地址、模型名和密钥。密钥加密保存在服务端。
2. 创建知识库，上传自己的课程资料并选择切分方式。规则切分不调用生成模型；模型语义切分需要真实生成模型。
3. 整理内容并发布知识库，在“应用管理”选择模型、绑定知识库，保存后发布。应用问答始终使用已发布范围。
4. 需要自主 Agent 时，在应用设置启用“自主学习助教”，选择支持 Tool Use 的模型；演示模型不执行 Agent。

**PDF 与图片 OCR** 使用已配置的 PaddleOCR 云端服务，需要在本地 `.env` 设置 `PADDLEOCR_TOKEN`，再重建服务使配置生效。默认模型为 `PaddleOCR-VL-1.6`，可用配置见 [.env.example](.env.example)。上传 PDF/图片会发送到该服务；没有 Token 时可先使用 TXT、Markdown、DOCX 或 PPTX 的文字内容。设置 `OCR_ENABLED=false` 会禁用 PDF/图片导入，不能切换到 PDF 文字层解析。

生成、Embedding 与 OCR 是独立配置。真实模型、模型文件、云端额度及课程资料需要自行准备；项目不会自动下载 Ollama 模型，也不会把云端失败自动切换为其他服务。

详细操作、工具清单、Skill 扩展、引用检查、请求恢复和限流说明见 [使用与运维说明](docs/usage.md)。

## 本地开发

建议使用 Python 3.12、[uv](https://github.com/astral-sh/uv) 和 Node.js 24。后端依赖由 `uv.lock` / `requirements.lock` 锁定，前端依赖由 `frontend/package-lock.json` 锁定。

```bash
uv sync --extra dev --python 3.12
npm --prefix frontend ci
```

以下 Bash 命令启动一个独立的 SQLite 演示环境，不需要 PostgreSQL、Redis 或真实模型。请从仓库根目录、在同一个终端执行：

```bash
mkdir -p data/development
export DATABASE_URL=sqlite:///./data/development/courseqa.db
export STORAGE_DIR=./data/development
export TASK_MODE=inline
export ALLOW_DEMO_PROVIDER=true
export SEMANTICA_ENABLED=false
export OCR_ENABLED=false
export COOKIE_SECURE=false
export PYTHONPATH=backend
export SECRET_KEY="$(.venv/bin/python -c 'import secrets; print(secrets.token_hex(32))')"
read -r -s -p '设置管理员密码（至少 12 个字符）: ' ADMIN_PASSWORD
export ADMIN_PASSWORD

.venv/bin/python -m app.cli init
.venv/bin/python -m app.cli demo
.venv/bin/uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

`init` 为 SQLite 创建表，`demo` 创建管理员与演示应用，两个步骤都需要执行。已有账号不会被覆盖。需要长期保留真实模型配置时，将固定的 `SECRET_KEY` 和开发配置保存在本地 `.env`；更换密钥会使已有加密凭据无法解密。SQLite 和 `TASK_MODE=inline` 适合开发，正式部署使用 PostgreSQL 和 Celery。

另开终端，在仓库根目录启动前端：

```bash
npm --prefix frontend run dev -- --host 127.0.0.1
```

访问 [http://127.0.0.1:5173](http://127.0.0.1:5173)。Vite 的 `/api` 代理固定指向 **`127.0.0.1:8000`**，与容器部署使用的 `8821` 不同；更改开发后端端口时同步修改 [vite.config.ts](frontend/vite.config.ts)。

### 测试与构建

```bash
.venv/bin/pytest -q
.venv/bin/ruff check backend scripts
npm --prefix frontend run build
```

常规测试使用临时数据库和模型替身，不需要真实模型密钥。真实 Ollama 集成测试需要已安装并运行的 Ollama，以及测试指定的 `bge-m3:567m`；默认连接 `127.0.0.1:11434`，可通过 `OLLAMA_TEST_URL` 调整：

```bash
ollama pull bge-m3:567m
RUN_OLLAMA_TESTS=1 .venv/bin/pytest -q backend/tests/test_integrations.py
```

浏览器测试另需安装 Chromium：

```bash
.venv/bin/python -m playwright install chromium
# Linux 缺少浏览器系统库时，可用 install --with-deps chromium 安装所需依赖
.venv/bin/python scripts/browser_agents.py
```

`browser_agents.py` 使用临时数据库和本地模拟模型。连接真实部署的脚本可能读取 `.env`、要求特定模型并写入演示记录，运行前请查看 [脚本说明](scripts/README.md)；真实质量评测的前置条件见 [评测说明](evaluation/README.md)。

## 目录结构

```text
backend/app/          API、数据模型、检索、Agent、工具与内置 Skills
backend/migrations/   Alembic 数据库迁移
backend/tests/        后端单元与集成测试
frontend/src/         Vue 页面、组件及流式客户端
deploy/               容器镜像与 Nginx 配置
scripts/              启动、导入、验证和评测脚本
evaluation/           评测与人工标注协议
docs/                 使用说明、设计材料和历史验证记录
.env.example          配置项示例，不含可用凭据
```

## 数据、文档与第三方材料

课程教材、上传文件、数据库、OCR 缓存、运行产物和真实学习记录**不随仓库发布**；`.env`、`data/`、`artifacts/`、原课程资料目录及依赖目录已从版本控制排除。演示应用使用代码中人工编写的示例，测试夹具用于软件验证；它们不是完整课程数据集或已审核的真实模型评测集。

- [使用与运维说明](docs/usage.md)：资料处理、Agent、Skills、问答接口、缓存与并发配置。
- [实验与数据标注](evaluation/README.md)：普通检索与 Agent 对比评测、人工评分方式。
- [脚本说明](scripts/README.md)：部署、临时环境验收和真实数据操作的适用范围。
- [历史验证记录](docs/validation.md)：特定环境下的检查结果，不是新部署的运行状态或性能承诺。
- [项目报告草稿](docs/project-report.md)、[演示脚本](docs/demo-script.md)：项目设计与展示材料，包含历史环境背景。

`sympy` 和 `scientific-critical-thinking` Skill 来自 [K-Dense 科学技能库](https://github.com/K-Dense-AI/claude-scientific-skills)。导入提交与文件校验值见 [imports.json](backend/app/skills/imports.json)，保留的上游 MIT 许可见 [THIRD_PARTY_LICENSE.md](backend/app/skills/THIRD_PARTY_LICENSE.md)。该许可对应导入的第三方材料，不代表整个项目采用同一许可。
