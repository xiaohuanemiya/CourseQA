"""Explicit operator reset of nonsemantic derived content, retaining original files and OCR.

Run inside the API container after backing up PostgreSQL; no active tasks are allowed.
The confirmation argument guards against accidentally re-running a destructive reset.
"""
import argparse
import json

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db import engine
from app.models import (Application, Audit, Document, EmbeddingIndex, Knowledge, KnowledgeBase, KnowledgeReview,
                        ModelProfile, Question, Relation, Release, ReleaseVector, Task, User)
from app.schemas import ParseOptions, SplitOptions
from app.services.splitting import write_json
from app.config import settings
from app.tasks import dispatch


def main(model_name):
    with Session(engine(), expire_on_commit=False) as db:
        if db.scalar(select(func.count()).select_from(Task).where(Task.state.in_(["pending", "running"]))):
            raise SystemExit("有未完成任务，未修改数据库")
        profiles = list(db.scalars(select(ModelProfile).where(ModelProfile.name == model_name)))
        if len(profiles) != 1 or profiles[0].provider == "demo" or profiles[0].purpose != "generation":
            raise SystemExit("必须存在唯一的真实生成模型配置")
        profile = profiles[0]
        if any((n.attributes or {}).get("splitting", {}).get("method") == "semantic"
               for n in db.scalars(select(Knowledge))):
            raise SystemExit("已存在语义切分结果，禁止再次清空；请重试失败任务")
        docs = list(db.scalars(select(Document).where(~Document.withdrawn).order_by(Document.filename)))
        if not docs:
            raise SystemExit("没有保留原文的文档，未修改数据库")
        classes = [KnowledgeReview, ReleaseVector, EmbeddingIndex, Release, Relation, Question, Knowledge]
        counts = {cls.__tablename__: db.scalar(select(func.count()).select_from(cls)) for cls in classes}
        actor = db.scalar(select(User).where(User.role == "admin"))
        for cls in classes:
            db.execute(delete(cls))
        for kb in db.scalars(select(KnowledgeBase)):
            kb.current_release_id = None
            kb.revision += 1
            if any(doc.kb_id == kb.id for doc in docs):
                kb.extraction_profile_id = profile.id
        for app in db.scalars(select(Application)):
            app.active = False
        for doc in docs:
            options = ParseOptions.model_validate(doc.parse_options or {})
            options.splitting = SplitOptions(method="semantic", model_profile_id=profile.id)
            doc.parse_options = options.model_dump()
            doc.state = "uploaded"
            doc.recognition = {k: v for k, v in (doc.recognition or {}).items() if k != "splitting"}
        db.add(Audit(actor_id=actor.id, entity_id="semantic-reimport", action="replace_nonsemantic_knowledge",
                     before=counts, after={"model_profile_id": profile.id, "documents": len(docs),
                     "reason": "用户要求删除原先非语义提取内容，使用 ds-flash 重新语义切分；保留原文与 OCR"}))
        db.commit()
        tasks = []
        for doc in docs:
            task = dispatch(db, "ingest", doc.id, actor.id)
            tasks.append({"document_id": doc.id, "filename": doc.filename, "task_id": task.id})
        report = {"removed": counts, "model": profile.model, "model_profile_id": profile.id,
                  "method": "semantic", "documents": tasks}
        write_json(settings().storage_dir / "semantic-reimport.json", report)
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", choices=["REPLACE_NON_SEMANTIC"], required=True)
    parser.add_argument("--model-name", default="ds-flash")
    args = parser.parse_args()
    main(args.model_name)
