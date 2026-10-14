"""Read-only agent comparisons; correctness remains unknown until human scoring."""
import hashlib
import json
import time
from collections import Counter
from datetime import datetime, timezone

from fastapi import HTTPException

from ..evaluation import summarize, validate_dataset
from ..models import ModelProfile, User
from ..schemas import AgentOptions, ChatInput
from . import learning, skills
from .providers import ProviderError

MODES = ("baseline", "tools", "multi_agent")


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def published_context(db, app_id):
    """Record reproducibility metadata without credentials or model response bodies."""
    app, releases = learning.scope(db, app_id, "chat")
    profile = db.get(ModelProfile, app.published["model_profile_id"])
    embedding = db.get(ModelProfile, app.published["embedding_profile_id"])
    if profile is None or embedding is None:
        raise ValueError("应用模型配置不存在")
    options = AgentOptions.model_validate(app.published.get("agent", {}))
    snapshot = app.published.get("skill_snapshot")
    available = {item["name"]: item for item in skills.runtime_catalog(snapshot)}
    metadata = {
        "app_id": app.id, "app_version": app.version,
        "configuration_sha256": fingerprint(app.published),
        "release_ids": [release.id for release, _ in releases],
        # Withdrawals change the usable snapshot without necessarily changing release IDs.
        "knowledge_sha256": fingerprint([snapshot for _, snapshot in releases]),
        "model": {"profile_id": profile.id, "provider": profile.provider,
                  "model": profile.model, "temperature": profile.temperature},
        "embedding": {"profile_id": embedding.id, "provider": embedding.provider,
                      "model": embedding.model, "dimension": embedding.dimension},
        "agent_options": options.model_dump(),
        "skill_snapshot": skills.snapshot_info(snapshot),
        "skills": [{"name": name, "sha256": available.get(name, {}).get("sha256"),
                    "package_sha256": available.get(name, {}).get("package_sha256")}
                   for name in options.skills],
    }
    return metadata, releases


def candidate_questions(releases, count=40):
    """Generate an annotation queue from published sources, never fabricated gold labels."""
    if not 30 <= count <= 50:
        raise ValueError("候选队列数量必须为 30–50")
    nodes = [{**node, "release_id": release.id} for release, snapshot in releases
             for node in snapshot["nodes"]]
    nodes.sort(key=lambda item: (item["release_id"], item["id"]))
    if not nodes:
        raise ValueError("当前应用没有可用于标注的已发布知识")
    rows = []
    kinds = ("single", "multi", "calculation", "unanswerable")
    for index in range(count):
        kind = kinds[index % len(kinds)]
        node = nodes[(index // len(kinds)) % len(nodes)]
        other = nodes[((index // len(kinds)) + 1) % len(nodes)]
        sources = [node]
        question = ""
        if kind == "single":
            question = f"请解释「{node['title']}」的含义与适用条件，并给出资料依据。"
        elif kind == "multi" and other["id"] != node["id"]:
            sources.append(other)
            question = f"请比较「{node['title']}」与「{other['title']}」的适用条件，并分别给出资料依据。"
        rows.append({
            "id": f"agent-{index + 1:03}", "group_id": f"source-{node['id']}",
            "type": kind, "split": "dev", "question": question,
            "answerable": kind != "unanswerable", "gold_answer": "", "evidence_titles": [],
            "reviewed": False, "candidate": True,
            "annotation_note": ("人工填写具有完整条件的计算题、标准答案及证据。" if kind == "calculation"
                                else "人工填写并核查在整个应用知识范围内无法回答的问题。" if kind == "unanswerable"
                                else "候选问题须核验相关性与可回答性，填写标准答案及证据。"),
            "source_candidates": [{"knowledge_id": source["id"], "title": source["title"],
                                   "release_id": source["release_id"], "excerpt": source["content"][:1600]}
                                  for source in sources],
        })
    return rows


def validate_agent_dataset(rows, split="test", allow_small=False):
    required = {"id", "group_id", "type", "split", "question", "answerable", "gold_answer",
                "evidence_titles", "reviewed"}
    for row in rows:
        if not isinstance(row, dict) or not required <= row.keys():
            raise ValueError("评测样本缺少字段，请使用现有问答标注 JSONL 格式")
        if row["reviewed"] is not True:
            raise ValueError(f"样本 {row['id']} 尚未人工审核，不能作为评测集")
        if not isinstance(row["answerable"], bool) or row["type"] not in {
            "single", "multi", "calculation", "unanswerable"
        }:
            raise ValueError("样本类型或可回答性标签无效")
        if not isinstance(row["question"], str) or not isinstance(row["evidence_titles"], list):
            raise ValueError("问题必须为文本，来源知识标题必须为列表")
        if any(not isinstance(title, str) for title in row["evidence_titles"]):
            raise ValueError("来源知识标题必须为文本")
    validate_dataset(rows, require_full=False)
    selected = [row for row in rows if row["split"] == split]
    if not selected:
        raise ValueError("所选 split 没有评测样本")
    if not allow_small and not 30 <= len(selected) <= 50:
        raise ValueError("Agent 对比默认需要所选 split 包含 30–50 道已审核题；小样本联调用 --allow-small")
    if any(len(row["question"]) > 4000 for row in selected):
        raise ValueError("问题不能超过 4000 字符")
    return selected


def _count_summary(results, field):
    values = [row["execution"].get(field) for row in results]
    known = [value for value in values if isinstance(value, (int, float)) and not isinstance(value, bool)]
    return {"total": sum(known) if len(known) == len(values) and values else None,
            "observed_total": sum(known) if known else None, "reported_count": len(known)}


def comparison_metrics(results, judgments=None):
    metrics = summarize(results, judgments)
    # An execution error is not a deliberate refusal and has no measured retrieval recall.
    successful = [row for row in results if row["error"] is None]
    if len(successful) != len(results):
        successful_metrics = summarize(successful)
        for field in ("refusal_precision", "refusal_recall"):
            metrics[field] = successful_metrics[field]
    recorded = [row for row in successful if row.get("retrieval_recorded")]
    metrics["evidence_recall_at_8"] = None
    # The agent can expand beyond eight items; retain the legacy field as unknown.
    metrics["evidence_recall"] = summarize(recorded)["evidence_recall_at_8"]
    metrics["retrieval_recorded_count"] = len(recorded)
    metrics.update({
        "success_count": len(successful), "error_count": len(results) - len(successful),
        "error_types": dict(Counter(row["error"]["type"] for row in results if row["error"])),
        "error_codes": dict(Counter(row["error"].get("code", "unknown") for row in results if row["error"])),
        "execution": {field: _count_summary(results, field)
                      for field in ("model_calls", "tool_calls", "subagents")},
        "usage_totals": {},
    })
    for field in ("prompt_tokens", "completion_tokens"):
        values = [{"execution": {field: (row.get("usage") or {}).get(field)}} for row in results]
        metrics["usage_totals"][field] = _count_summary(values, field)
    latencies = sorted(row["latency_seconds"] for row in results)
    metrics["p95_latency_seconds"] = latencies[max(0, (95 * len(latencies) + 99) // 100 - 1)] if latencies else None
    return metrics


def score_report(report, judgments):
    """Apply independent reviewed judgments without changing execution measurements."""
    results = report["results"]
    identifiers = {row["id"] for row in results}
    labels = {}
    for row in judgments:
        if row.get("id") not in identifiers or row["id"] in labels:
            raise ValueError("人工评分 ID 不在结果中或有重复")
        if row.get("reviewed") is not True or not isinstance(row.get("correct"), bool):
            raise ValueError("人工评分必须 reviewed=true 并明确 correct=true/false")
        if row.get("citations_supported") is not None and not isinstance(row["citations_supported"], bool):
            raise ValueError("citations_supported 必须为 true、false 或 null")
        labels[row["id"]] = row
    return {**report, "metrics": comparison_metrics(results, labels),
            "judgments": list(labels.values()), "judgments_sha256": fingerprint(judgments)}


async def evaluate_dataset(db, app_id, user_id, rows, *, split="test", allow_small=False,
                           evaluator=None, progress=None):
    """Run all modes over identical reviewed questions without writing student messages.

    The caller supplies a dedicated read-only session. ``progress`` may persist checkpoints.
    Calls are interleaved by question with rotating mode order to reduce ordering bias.
    """
    selected = validate_agent_dataset(rows, split, allow_small)
    if db.get(User, user_id) is None:
        raise ValueError("评测用户不存在")
    metadata, releases = published_context(db, app_id)
    if metadata["model"]["provider"] == "demo":
        raise ValueError("Agent 对比需要真实生成模型，演示证据模式不能代替模型实验")
    available = {node["title"] for _, snapshot in releases for node in snapshot["nodes"]}
    for row in selected:
        if not set(row["evidence_titles"]) <= available:
            raise ValueError(f"样本 {row['id']} 的标准证据不在此应用的知识范围内")
    if evaluator is None:
        from .agents import evaluate_answer
        evaluator = evaluate_answer
    metadata.update({"dataset_sha256": fingerprint(rows), "split": split,
                     "started_at": datetime.now(timezone.utc).isoformat(),
                     "ordering": "question-interleaved, rotating mode order",
                     "selected_count": len(selected)})
    baseline_version = {key: value for key, value in metadata.items()
                        if key not in {"dataset_sha256", "split", "started_at", "ordering", "selected_count"}}
    report = {"metadata": metadata, "modes": {
        mode: {"results": [], "metrics": comparison_metrics([])} for mode in MODES}, "complete": False}
    for index, row in enumerate(selected):
        rotated = MODES[index % len(MODES):] + MODES[:index % len(MODES)]
        for mode in rotated:
            db.expire_all()
            current, _ = published_context(db, app_id)
            if current != baseline_version:
                raise ValueError("评测期间应用、模型、Skill 或知识版本已变化；请使用新的输出目录重新评测")
            start = time.perf_counter()
            response, error, execution = {}, None, {}
            try:
                response = await evaluator(db, app_id, user_id, ChatInput(message=row["question"]), mode=mode)
                execution = response.get("agent") or {}
            except (ProviderError, HTTPException, TimeoutError) as exc:
                execution = getattr(exc, "execution", None) or {}
                # Avoid exporting upstream response bodies, credentials, or private reasoning.
                error = {"type": type(exc).__name__, "message": "本次调用失败；请结合执行记录排查后重试。"}
                error["code"] = getattr(exc, "code", "timeout" if isinstance(exc, TimeoutError) else "request_error")
                if isinstance(exc, HTTPException):
                    error["status_code"] = exc.status_code
            elapsed = time.perf_counter() - start
            db.expire_all()
            current, _ = published_context(db, app_id)
            if current != baseline_version:
                raise ValueError("评测期间应用、模型、Skill 或知识版本已变化；请使用新的输出目录重新评测")
            result = {
                "id": row["id"], "group_id": row["group_id"], "mode": mode, "type": row["type"],
                "question": row["question"], "gold_answer": row["gold_answer"], "answerable": row["answerable"],
                "gold_evidence": row["evidence_titles"], "status": response.get("status", "error"),
                "answer": response.get("answer", ""), "citations": response.get("citations", []),
                "retrieved_titles": response.get("retrieved_titles", []),
                "latency_seconds": elapsed, "usage": execution.get("usage"), "execution": execution,
                "release_ids": metadata["release_ids"], "model": metadata["model"]["model"], "error": error,
            }
            # Final citations are not the complete retrieval set. Do not report them as recall.
            if "retrieved_titles" not in response:
                result["retrieval_recorded"] = False
            else:
                result["retrieval_recorded"] = True
            output = report["modes"][mode]
            output["results"].append(result)
            output["metrics"] = comparison_metrics(output["results"])
            if progress:
                progress(report)
    report["complete"] = True
    if progress:
        progress(report)
    return report
