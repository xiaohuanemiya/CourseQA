"""Offline importance and recommendation comparisons against independent human annotations."""
import argparse
import json
from pathlib import Path
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import engine
from app.evaluation import ndcg
from app.models import Attempt, User
from app.services.learning import review_plan, scope


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("annotations",type=Path)
    parser.add_argument("--app-id",required=True)
    parser.add_argument("--user-id",help="Optional consenting participant's local account ID")
    parser.add_argument("--output",type=Path,default=Path("artifacts/evaluation/learning.json"))
    args=parser.parse_args()
    gold=json.loads(args.annotations.read_text())
    if not gold.get("importance"):
        raise SystemExit("需要独立的重要性标注；不能把平台分数当作标准答案")
    with Session(engine()) as db:
        _,releases=scope(db,args.app_id)
        titles={n["id"]:n["title"] for _,snapshot in releases for n in snapshot["nodes"]}
        scores={key:value for _,snapshot in releases for key,value in snapshot["importance"].items() if key in titles}
        if not set(gold["importance"])<=set(titles) or any(not isinstance(v,(int,float)) or v<0 for v in gold["importance"].values()):
            raise SystemExit("重要性标签必须是本应用知识 ID 对应的非负分数")
        output={"release_ids":[r.id for r,_ in releases],"importance":{}}
        for name,field in [("frequency","document_frequency"),("pagerank","pagerank"),("fusion","score")]:
            ranked=sorted(scores,key=lambda key:(-scores[key][field],key))
            output["importance"][name]={"ndcg_at_10":ndcg(ranked,gold["importance"]),"top_10":[titles[k] for k in ranked[:10]]}
        if args.user_id:
            user=db.get(User,args.user_id)
            weak=set(gold.get("weak_knowledge_ids",[]))
            if not user or not weak or not weak<=set(titles):
                raise SystemExit("复习对比需要有效用户和独立标注的本应用薄弱知识 ID")
            attempts=db.scalars(select(Attempt).where(Attempt.user_id==user.id,Attempt.app_id==args.app_id,
                                                    Attempt.verdict=="incorrect")).all()
            errors=Counter(k for a in attempts for k in a.knowledge_ids if k in titles)
            plans={"mistakes_only":[k for k,_ in errors.most_common(10)],
                   "with_prerequisites":[r["id"] for r in review_plan(db,args.app_id,user)["items"][:10]]}
            output["review"]={name:{"weak_point_coverage_at_10":len(set(keys)&weak)/len(weak),
                                      "top_10":[titles[k] for k in keys]} for name,keys in plans.items()}
            output["limitation"]="离线覆盖率不等同于学习成绩改善；用户评价与后续作答仍需实际采集。"
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(output,ensure_ascii=False,indent=2))
        print("离线结果已保存：",args.output)


if __name__=="__main__":
    main()
