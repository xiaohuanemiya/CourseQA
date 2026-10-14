# 脚本导航

所有命令均从仓库根目录运行。Python 示例使用已安装项目及开发依赖的 `.venv`；直接导入 `app` 的脚本需要 `PYTHONPATH=backend`。浏览器验收还需要 Playwright Chromium 和已构建的前端。安装与环境配置见[项目 README](../README.md)。

只有下文明确标为“临时数据库”的脚本会自行隔离数据。其余脚本可能读取当前目录的 `.env`、连接其中配置的数据库，或使用管理员密码访问现有部署；查看脚本名称中的 `verify` / `smoke` 不能判断它是否会写数据。截图、报告和标注文件通常输出到 `artifacts/`。

## 部署与更新

| 脚本 | 用途与数据影响 |
|---|---|
| [podman-up.sh](podman-up.sh) | 首次构建并启动项目容器，执行数据库迁移并创建演示数据；已有同名容器时退出。 |
| [bootstrap_env.py](bootstrap_env.py) | 首次生成权限为 `0600` 的 `.env`；不覆盖已有文件，不输出密码。`podman-up.sh` 已调用它。 |
| [podman-rebuild.sh](podman-rebuild.sh) | 重建镜像，替换 API、Worker、Web 容器并升级数据库；保留数据卷。升级前备份 PostgreSQL 与 `calculus-data`，便于恢复数据库及原始文件的一致状态。 |
| [podman-down.sh](podman-down.sh) | 停止本项目五个容器，保留容器和数据卷。 |

```bash
bash scripts/podman-up.sh
# 后续代码更新使用：
bash scripts/podman-rebuild.sh
```

## 本地模拟验收：临时数据库，无云端调用

[browser_agents.py](browser_agents.py) 自建临时 SQLite、本地 API 和模拟模型，验证 Skill、Tool Use、子 agent、流式回答及断线查询；[browser_relation_generation.py](browser_relation_generation.py) 用同样的隔离方式验证关系生成。两者不连接部署数据库、不调用云端模型，结束后删除临时数据，保留截图与报告。

```bash
npm --prefix frontend run build
.venv/bin/python scripts/browser_agents.py
.venv/bin/python scripts/browser_relation_generation.py
```

[audit_materials.py](audit_materials.py)、[verify_material_parser.py](verify_material_parser.py)、[prepare_annotations.py](prepare_annotations.py) 只读取本地资料并写检查报告或待标注文件，不调用生成模型或云端 OCR。[prepare_ocr_fixture.py](prepare_ocr_fixture.py) 使用浏览器和本地 KaTeX 生成合成扫描图片/PDF，本身不提交识别任务。

```bash
.venv/bin/python scripts/audit_materials.py /path/to/materials
.venv/bin/python scripts/prepare_annotations.py /path/to/materials
```

## 真实部署验收：先核对脚本中的目标

以下浏览器/API 脚本读取 `.env` 中的管理员密码，通常连接 `http://127.0.0.1:8820`。`browser_smoke.py`、`browser_batch_upload.py` 支持 `BROWSER_BASE_URL`；其余多有固定地址、课程名称、应用 ID 或已有 `artifacts/` 文件要求，需要先按目标部署核对，不能作为新安装环境的通用测试集。

| 脚本 | 实际操作与服务调用 |
|---|---|
| [browser_smoke.py](browser_smoke.py) | 登录、发送问答、生成自测并提交答案，会留下会话和作答；预期演示应用，实际模型调用取决于该应用当前配置。 |
| [browser_batch_upload.py](browser_batch_upload.py)、[browser_no_review_smoke.py](browser_no_review_smoke.py) | 在真实部署创建临时知识库、上传文本并编辑；后者还发布版本。结束时删除临时知识库；按脚本指定的文本与非模型切分流程，不调用云端模型。 |
| [browser_admin_learning_smoke.py](browser_admin_learning_smoke.py) | 读取管理端统计并创建临时学生验证权限；通过 `podman exec calculus-api` 清理该账号，需本机容器与对应课程数据。 |
| [browser_graph_smoke.py](browser_graph_smoke.py)、[browser_splitting_smoke.py](browser_splitting_smoke.py)、[browser_ocr_smoke.py](browser_ocr_smoke.py) | 登录后查看已有图谱、切分报告或 OCR 结果，不提交新的模型/OCR任务；登录会创建会话。 |
| [browser_streaming_smoke.py](browser_streaming_smoke.py) | 登录真实部署，但拦截问答流并使用本地数据包；不调用云端模型，依赖既有课程 ID 和报告样本。 |
| [verify_paddleocr.py](verify_paddleocr.py) | 上传合成扫描图片并等待真实 PaddleOCR 任务；会使用已配置的云端 OCR，保留验收知识库与文档，不自动发布。须先运行 `prepare_ocr_fixture.py`。 |
| [verify_exam_retrieval.py](verify_exam_retrieval.py) | 直接读取配置的部署数据库，核对特定历年试卷检索；只允许本机 Ollama Embedding，不调用生成模型，不改课程数据。 |

## 评测与人工标注

数据格式、分组规则和人工评分见[评测说明](../evaluation/README.md)。`prepare_annotations.py` 生成的 240 个槽位，以及下面 `prepare` 生成的 30–50 个候选，都需要人工填写与核验，不能直接当作已审核题集。

```bash
# 读取已发布知识；不调用模型，不修改发布配置。
.venv/bin/python scripts/evaluate_agents.py prepare --app-id APPLICATION_ID --count 40
# 人工完成题集后，显式调用该应用的真实生成/Embedding 模型：
.venv/bin/python scripts/evaluate_agents.py run artifacts/annotation/agent-reviewed.jsonl \
  --app-id APPLICATION_ID --user-id EVALUATOR_ACCOUNT_ID --split test \
  --output artifacts/evaluation/agents-run-001
```

`evaluate_agents.py run` 比较普通问答、仅工具、允许子 agent 三种模式，不保存学生消息、不改应用发布配置；会消耗真实模型额度。报告记录版本、调用量、耗时和失败，准确率与引用支持率等待独立人工评分，未知 Token 用量保留空值。`score` 子命令只合并评分，不调用模型。

[evaluate.py](evaluate.py) 是 direct/vector/hybrid/graph 四组检索对比，会调用真实生成及 Embedding 模型；[score_evaluation.py](score_evaluation.py) 只处理本地结果和人工评分。[evaluate_learning.py](evaluate_learning.py) 读取数据库与独立人工标签，比较重要性排序或薄弱点覆盖，不运行生成模型、不代表真实学习成绩提升。这三类旧入口需 `PYTHONPATH=backend`。

## 资料导入与模型维护

[import_materials.py](import_materials.py) 通过真实管理 API 新建或复用“高数”知识库并提交导入任务；读取 `.env`，可用 `--base-url` 指定部署。批量扫描 PDF、DOCX、PPTX、MD、TXT，默认语义切分需要 `--extraction-profile-id` 指向真实生成模型。PDF 识别使用部署的 PaddleOCR；`--raw-only` 只关闭生成模型切分，不会绕过 PDF 的 OCR，也不代表离线导入。脚本不自动发布知识库。

```bash
.venv/bin/python scripts/import_materials.py /path/to/materials \
  --extraction-profile-id GENERATION_PROFILE_ID --split-method semantic
```

[register_local_models.py](register_local_models.py) 登录真实部署、发现本地 Ollama 向量模型，写入配置并实际测试；[configure_demo_embeddings.py](configure_demo_embeddings.py) 修改两个指定演示应用的 Embedding 配置并重新发布、发起验证问答。后者会改变学生当前看到的应用版本，执行前保存数据库备份以便恢复原发布配置。

## 清空与重建：仅用于明确的资料维护

这两项直接操作部署数据库，应在 API 容器内运行。执行前完成或处理所有 `pending` / `running` 任务、停止 Worker 并暂停上传与问答，先备份 PostgreSQL、`calculus-data` 文件卷及本地原始资料；恢复时数据库中的文档路径必须与文件卷对应。脚本检测到未完成任务会拒绝执行，停止 Worker 本身不会把任务改成完成。

| 脚本与确认参数 | 会修改什么、何时使用 |
|---|---|
| [reset_knowledge.py](reset_knowledge.py) `--confirm DELETE_ALL_KNOWLEDGE` | 清空所有知识、文档记录、关系、题库、发布快照和向量索引，删除相关任务/审计记录，清除知识库当前版本并停用全部应用。用于明确决定从原始资料全部重新导入；保留文件卷不等于保留数据库中的文档记录。 |
| [semantic_reimport.py](semantic_reimport.py) `--confirm REPLACE_NON_SEMANTIC --model-name MODEL_NAME` | 删除旧派生知识、题库、关系、发布与索引，保留原文件和 OCR，设置语义切分并为未撤回文档提交重建任务，停用全部应用。需要唯一的真实生成模型；已有语义结果时拒绝再次清空。重建任务会调用生成模型，缺少可复用识别结果时也可能调用 OCR；任务入队后再启动 Worker。 |
| [refresh_unreviewed_materials.py](refresh_unreviewed_materials.py) | 已停用，运行会直接退出并提示使用知识库“原始资料 → 重新解析切分”，不会刷新数据。新流程生成独立文档版本；整理后撤回旧内容并重新发布。 |

[verify_semantic_reimport.py](verify_semantic_reimport.py) 读取重建任务清单和真实部署结果，对照原文/OCR 检查覆盖，不调用模型；需要对应的 `artifacts/semantic-reimport-tasks.json` 与原始资料目录。[verify_reimport.py](verify_reimport.py) 是旧重置流程验收，仍断言未发布和旧审核字段，不能用它作为当前日常发布流程的验收标准。
