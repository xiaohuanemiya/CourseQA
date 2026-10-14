# 实验与数据标注

本目录提供可复现的实验流程；真实的问答准确率需要云端或本地生成模型，以及人工核验的测试集。

## 数据格式

`scripts/prepare_annotations.py` 从用户资料采样 100 个真实页面片段供抽取标注，并生成 240 个**尚未填写、未经审核**的问答槽位。不能把这些槽位计为已完成数据集。

每行 JSON 包含 `id`、`group_id`、`type`、`split`、`question`、`answerable`、`gold_answer`、`evidence_titles`、`reviewed`。类型配额为 single 80、multi 60、calculation 60、unanswerable 40；开发集 60、测试集 180。相同原题及其变式必须使用相同 group_id 并放在同一 split。

资料中出现答案本身是检索实验的正常设定；测试问题、标准答案标注及人工评分不得用于调整提示词、阈值和权重。题库练习与测试题应按原题家族去重，并记录资料与模型版本。

抽取标注队列中的 `gold_entities`、`gold_relations` 和 `formula_notes` 由人工对照 PDF 填写。对定义、定理条件和公式的错误单独分类，不把页面文字数量当成知识质量。

## 运行问答对比

在已配置真实模型、已发布知识库的应用上运行：

```bash
python scripts/evaluate.py artifacts/annotation/questions.jsonl --app-id APPLICATION_ID --split test
```

脚本按相同应用生成模型依次运行 direct、vector、hybrid、graph 四组：分别为无检索、纯向量、词项与向量混合，以及混合检索加图扩展。三组检索最多使用 8 条证据，每条最多 2500 字符；图扩展最多两跳。direct 不提供检索证据。运行不写学生学习记录。先用小规模人工审核数据验证流程时加 `--allow-small`。

输出包含问题、标准答案、检索标题、回答、拒答状态、延迟、提供方返回的 Token 用量及知识发布版本。模型不返回用量时保存空值，不估算为真实用量。

准确率与引用支持率初始为空，需人工评分后计算。每个实验结果分别配一份判断 JSONL：

```json
{"id":"single-001","reviewed":true,"correct":true,"citations_supported":true}
```

```bash
python scripts/score_evaluation.py artifacts/evaluation/graph.json judgments-graph.jsonl --output artifacts/evaluation/graph-scored.json
```

建议两名标注者独立评估，分歧复核。公式题可使用数学工具辅助；不能单靠同一个生成模型给自己的回答打分。

## Agent、Skill 和工具使用对比

`scripts/evaluate_agents.py` 使用同一已发布应用、生成模型、知识范围和人工审核问题，比较三个模式：

| 模式 | 行为 |
| --- | --- |
| `baseline` | 普通问答，保留相同应用检索，不启用 Agent 工具循环 |
| `tools` | 启用 Skill 和工具，临时将子 agent 数量上限设为 0 |
| `multi_agent` | 启用 Skill、工具和应用配置允许的子 agent，由模型决定是否拆分 |

这些覆盖只作用于本次评测的内存配置，不改变应用发布配置，不保存会话、学生消息或作答记录。`multi_agent` 继承应用的子 agent 上限；上限为 0 时与 `tools` 相同，报告保留配置以便发现这种情况。只读的候选生成与会调用真实模型的 `run` 是两个独立命令；本项目没有随代码附带或声称取得真实模型评测成绩。

先从当前应用的已发布知识生成 30–50 个候选或待填写槽位（默认 40）。该步骤不调用模型：

```bash
.venv/bin/python scripts/evaluate_agents.py prepare --app-id APPLICATION_ID --count 40 \
  --output artifacts/annotation/agent-candidates.jsonl
```

候选包含来源知识 ID、标题、发布版本和正文片段；单知识与比较题提供可修改的问题模板，计算题与不可回答题留待人工编写。全部标记 `reviewed:false`，标准答案和标准证据均留空，**不是已审核的真实课程问答题集**。同一来源的候选使用相同 `group_id`，初始均放入 `dev`；标注者仍须按原题家族检查重复、相关知识及变式，不能只按来源 ID 认定独立。确定真实问题、完整条件、可回答性、标准答案及证据后设置 `reviewed:true`，按家族分配 `dev` / `test`。与已有 240 题格式兼容，也可以从既有审核题集中选取 30–50 道，注意保持分组边界。

执行真实模型对比需要显式运行下列命令；会使用应用配置的模型额度：

```bash
.venv/bin/python scripts/evaluate_agents.py run artifacts/annotation/agent-reviewed.jsonl \
  --app-id APPLICATION_ID --user-id EVALUATOR_ACCOUNT_ID --split test \
  --output artifacts/evaluation/agents-run-001
```

只接受 `reviewed:true` 的完整标签，不接受演示模型。默认要求所选 split 包含 30–50 题；少量已审核样本联调用 `--allow-small`。建议包括简单单知识问答、需要计算或核验的题、跨资料比较及证据不足问题，避免只挑适合多 agent 的难题。应用的模型与知识必须预先配置并发布，脚本不会自动调整它们。

每道问题轮流执行三种模式，并轮换模式先后次序，以减轻固定执行顺序带来的偏差。输出 `baseline.json`、`tools.json`、`multi_agent.json` 与 `comparison.json`，每次调用后保存检查点；`complete:false` 表示流程尚未全部完成。已有输出文件不会被覆盖。报告记录：

- 应用/知识版本、配置与知识内容哈希、模型参数、Skill 哈希、题集哈希及开始时间；任一版本在评测中变化即停止，避免混合比较。
- 回答、引用、拒答状态、错误类别、总延迟、模型调用次数、工具调用次数、子 agent 数量及执行记录。
- 提供方返回的 Token 用量。任一调用未提供某项用量时，完整总量为 `null`；`observed_total` 与 `reported_count` 仅描述确实返回的数据，不补零、不推算。

准确率与引用支持率始终保持 `null`，直到独立人工评分。沿用上面的 judgment JSONL 格式，对三个模式分别执行（不调用模型）：

```bash
.venv/bin/python scripts/evaluate_agents.py score artifacts/evaluation/agents-run-001/multi_agent.json \
  judgments-multi-agent.jsonl --output artifacts/evaluation/agents-run-001/multi_agent-scored.json
```

评分必须 `reviewed:true`，未评分题不混入已评分正确率，报告保留评分覆盖数；执行错误也应人工判为未正确回答，避免仅统计成功调用。Agent 可累积超过 8 条检索证据，因此比较文件的 `evidence_recall` 表示全部已记录检索的覆盖率，旧字段 `evidence_recall_at_8` 留空；不要把最终引用数量或执行记录存在当作引用真实性的证明。

该入口评测独立的单次问题，不注入学生对话历史。多轮追问质量需另行标注连续对话。耗时、调用量和工具选择可以自动比较；是否准确、引用是否支持结论以及多 agent 是否值得成本，必须结合人工评分与真实模型运行结果判断。

## 排序与复习实验

`app.evaluation.ndcg` 支持计算人工重要性分级下的 NDCG@10。分别导出文档覆盖频次、PageRank 和融合分数的排序，对比同一份人工清单。初始融合权重为 0.3/0.3/0.4；没有往年题时前两项各 0.5。权重只在开发资料上调整。

`scripts/evaluate_learning.py` 可直接运行三种排序，以及“仅错题”和“加入前置关系”的薄弱点覆盖率对比。人工标签采用知识 ID，`importance` 为非负重要性评分，`weak_knowledge_ids` 必须来自独立诊断：

```json
{"reviewed":true,"importance":{"KNOWLEDGE_ID":3},"weak_knowledge_ids":["KNOWLEDGE_ID"]}
```

```bash
python scripts/evaluate_learning.py learning-labels.json --app-id APPLICATION_ID --user-id PARTICIPANT_ID
```

省略 `--user-id` 时只运行重要性排序。脚本读取配置中的数据库；在容器内执行时使用 `/app/scripts/` 下的同名脚本，并将已审核标注文件挂载进去。不要用推荐系统自身的掌握程度作为薄弱点标准答案。

复习推荐的正式学习效果需真实学生参与及前后测设计。当前平台保存隔离的作答记录，支持“错误优先”和“错误加前置知识”方案的离线分析；没有足够参与者时，只报告覆盖率、排序差异和探索性反馈。

验收目标不是实测结果。报告需同时给出样本量、人工评分覆盖率、失败案例、拒答精确率与召回率，以及图检索带来的时延变化。
