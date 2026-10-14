"""Run equal-budget direct / vector / hybrid / graph baselines without changing records."""
import argparse
import json
import time
from pathlib import Path

from sqlalchemy.orm import Session

from app.db import engine
from app.evaluation import summarize, validate_dataset
from app.models import ModelProfile
from app.services.learning import retrieve, scope
from app.services.providers import generate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset",type=Path)
    parser.add_argument("--app-id",required=True)
    parser.add_argument("--split",choices=["dev","test"],default="test")
    parser.add_argument("--output",type=Path,default=Path("artifacts/evaluation"))
    parser.add_argument("--allow-small",action="store_true")
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.dataset.read_text().splitlines() if line.strip()]
    validate_dataset(rows,require_full=not args.allow_small)
    args.output.mkdir(parents=True,exist_ok=True)
    with Session(engine()) as db:
        app, releases = scope(db,args.app_id)
        profile = db.get(ModelProfile,app.published["model_profile_id"])
        if profile.provider == "demo":
            raise SystemExit("正式问答对比需要真实生成模型，演示证据模式不能代替模型实验")
        available = {n["title"] for _,snapshot in releases for n in snapshot["nodes"]}
        for row in rows:
            if not set(row.get("evidence_titles",[])) <= available:
                raise SystemExit(f"样本 {row['id']} 的标准证据不在此应用的知识范围内")
        for mode in ["direct","vector","hybrid","graph"]:
            output=[]
            for row in rows:
                if row["split"] != args.split:
                    continue
                start=time.perf_counter()
                evidence=[] if mode=="direct" else retrieve(db,app,releases,row["question"],
                    use_graph=mode=="graph",vector_only=mode=="vector")[:8]
                context=[{"title":e["title"],"content":e["content"][:2500]} for e in evidence]
                response=generate(profile,"严谨回答高数问题。不知道时拒答。返回 JSON：status(answered/insufficient_evidence/needs_input)、answer、citations 数组。",
                    json.dumps({"question":row["question"],"evidence":context},ensure_ascii=False),structured=True)
                output.append({"id":row["id"],"mode":mode,"type":row["type"],"question":row["question"],
                    "gold_answer":row.get("gold_answer"),"answerable":row["answerable"],"gold_evidence":row.get("evidence_titles",[]),
                    "retrieved_titles":[e["title"] for e in evidence],"status":response["status"],"answer":response["answer"],
                    "citations":response.get("citations",[]),"latency_seconds":time.perf_counter()-start,
                    "usage":response.get("_usage"),"release_ids":[r.id for r,_ in releases],"model":profile.model})
            (args.output/f"{mode}.json").write_text(json.dumps({"results":output,"metrics":summarize(output)},ensure_ascii=False,indent=2))
            print(mode,"完成",len(output),"题；正确率和引用支持率等待人工评分")


if __name__=="__main__":
    main()
