"""Offline evaluation support. Accuracy and citation support require independent human labels."""
import math
from collections import Counter


def validate_dataset(rows, require_full=True):
    if not rows:
        raise ValueError("评测样本不能为空")
    if len({r["id"] for r in rows}) != len(rows):
        raise ValueError("评测样本 ID 重复")
    groups = {}
    for row in rows:
        if not row.get("question", "").strip() or not row.get("group_id"):
            raise ValueError("评测样本必须填写问题与原题分组")
        if row["split"] not in {"dev", "test"}:
            raise ValueError("split 必须为 dev 或 test")
        if row["group_id"] in groups and groups[row["group_id"]] != row["split"]:
            raise ValueError("同一原题及其变式泄漏到不同分组")
        groups[row["group_id"]] = row["split"]
        if row["answerable"] and (not row.get("gold_answer") or not row.get("evidence_titles")):
            raise ValueError("可回答样本需要标准答案与来源知识标题")
    if require_full:
        if Counter(r["type"] for r in rows) != {"single":80,"multi":60,"calculation":60,"unanswerable":40}:
            raise ValueError("正式评测需要 80/60/60/40 共 240 个样本")
        if Counter(r["split"] for r in rows) != {"dev":60,"test":180}:
            raise ValueError("正式评测需要开发集 60、测试集 180")


def summarize(results, judgments=None):
    judgments = judgments or {}
    refusal_tp = refusal_fp = refusal_fn = 0
    recalls, latencies, correctness, supports = [], [], [], []
    for row in results:
        refused = row["status"] == "insufficient_evidence"
        refusal_tp += int(refused and not row["answerable"])
        refusal_fp += int(refused and row["answerable"])
        refusal_fn += int(not refused and not row["answerable"])
        if row["answerable"] and row["gold_evidence"]:
            recalls.append(len(set(row["retrieved_titles"]) & set(row["gold_evidence"]))/len(set(row["gold_evidence"])))
        latencies.append(row["latency_seconds"])
        judgment = judgments.get(row["id"])
        if judgment and isinstance(judgment.get("correct"), bool):
            correctness.append(int(judgment["correct"]))
            if judgment.get("citations_supported") is not None:
                supports.append(int(judgment["citations_supported"]))
    def mean(values):
        return sum(values)/len(values) if values else None
    return {"count":len(results), "accuracy":mean(correctness), "human_judged":len(correctness),
            "citation_support_rate":mean(supports), "evidence_recall_at_8":mean(recalls),
            "refusal_precision":refusal_tp/(refusal_tp+refusal_fp) if refusal_tp+refusal_fp else None,
            "refusal_recall":refusal_tp/(refusal_tp+refusal_fn) if refusal_tp+refusal_fn else None,
            "mean_latency_seconds":mean(latencies),
            "token_usage": [r.get("usage") for r in results]}


def ndcg(ranked, gold, k=10):
    gain = sum((2**gold.get(item,0)-1)/math.log2(i+2) for i,item in enumerate(ranked[:k]))
    ideal = sum((2**score-1)/math.log2(i+2) for i,score in enumerate(sorted(gold.values(),reverse=True)[:k]))
    return gain/ideal if ideal else 0
