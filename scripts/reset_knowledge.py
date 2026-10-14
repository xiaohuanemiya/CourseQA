"""Operator-only reset. Run inside calculus-api after a database backup and with worker stopped."""
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db import engine
from app.models import (Application, Audit, Document, EmbeddingIndex, Knowledge, KnowledgeBase, KnowledgeReview,
                        Question, Relation, Release, ReleaseVector, Task, User)


def main():
    with Session(engine()) as db:
        if db.scalar(select(func.count()).select_from(Task).where(Task.state.in_(["pending","running"]))):
            raise SystemExit("存在未完成任务，停止任务后才能重置")
        classes=[KnowledgeReview,ReleaseVector,EmbeddingIndex,Release,Relation,Question,Knowledge,Document]
        counts={cls.__tablename__:db.scalar(select(func.count()).select_from(cls)) for cls in classes}
        ids={row for cls in classes for row in db.scalars(select(cls.id))}
        db.execute(delete(Audit).where(Audit.entity_id.in_(ids)))
        db.execute(delete(KnowledgeReview))
        db.execute(delete(Task).where(Task.target_id.in_(ids)))
        for cls in classes:
            db.execute(delete(cls))
        for kb in db.scalars(select(KnowledgeBase)):
            kb.current_release_id=None
            kb.revision+=1
        for app in db.scalars(select(Application)):
            app.active=False
        actor=db.scalar(select(User).where(User.role=="admin"))
        db.add(Audit(actor_id=actor.id,entity_id="knowledge-reset",action="reset_all_knowledge",
            before=counts,after={"reason":"用户要求清空全部知识并重新导入原数学资料","published":False}))
        db.commit()
        print("清空完成：",counts)


if __name__=="__main__":
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--confirm',choices=['DELETE_ALL_KNOWLEDGE'],required=True)
    parser.parse_args()
    main()
