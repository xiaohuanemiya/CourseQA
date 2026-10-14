from celery import Celery
from celery.signals import task_failure
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .config import settings
from .db import engine
from .models import Task
from .services.ingestion import ingest
from .services.knowledge import publish
from .services.providers import ProviderError
from .services.learning import publish_application

celery = Celery("calculus", broker=settings().redis_url)
celery.conf.update(task_serializer="json", accept_content=["json"], worker_prefetch_multiplier=1,
                   task_time_limit=4200, task_soft_time_limit=4140)


@task_failure.connect
def record_worker_failure(sender=None, args=None, **kwargs):
    if sender and sender.name == "calculus.run_task" and args:
        with Session(engine()) as db:
            task = db.get(Task, args[0])
            if task and task.state == "running":
                task.state, task.error = "failed", "后台任务中断或超时，请在任务中心重试"
                db.commit()


@celery.task(name="calculus.run_task")
def run_task(task_id):
    with Session(engine(), expire_on_commit=False) as db:
        claimed = db.execute(update(Task).where(Task.id == task_id, Task.state == "pending")
                             .values(state="running", progress=10))
        db.commit()
        if claimed.rowcount != 1:
            return
        task = db.get(Task, task_id)
        try:
            def progress(value, result=None):
                with Session(engine()) as status_db:
                    values = {"progress": value}
                    if result is not None:
                        values["result"] = result
                    status_db.execute(update(Task).where(Task.id == task_id, Task.state == "running").values(**values))
                    status_db.commit()
            if task.kind == "publish":
                result = publish(db, task.target_id, task.actor_id)
            elif task.kind == "ingest":
                result = ingest(db, task.target_id, task.actor_id, progress=progress)
            elif task.kind == "generate_relations":
                from .services.relation_generation import generate_relations
                result = generate_relations(db, task.target_id, task.actor_id, task.payload, task.id, progress)
            elif task.kind == "auto_review":
                task.state, task.error = "cancelled", "审核流程已移除，此历史任务不再执行"
                db.commit()
                return
            elif task.kind == "publish_app":
                result = publish_application(db, task.target_id, task.actor_id)
            else:
                raise ValueError("未知任务")
            task.state, task.progress, task.result = "succeeded", 100, result
        except Exception as exc:
            db.rollback()
            task = db.get(Task, task_id)
            task.state = "failed"
            task.error = str(exc)[:500] if isinstance(exc, (ValueError, ProviderError)) else "处理失败，请查看服务日志"
            import logging
            logging.getLogger(__name__).error("Task %s failed (%s)", task_id, type(exc).__name__)
        db.commit()


def dispatch(db, kind, target_id, actor_id, payload=None):
    if kind == "auto_review":
        raise ValueError("审核流程已移除")
    existing = next((t for t in db.scalars(select(Task).where(Task.target_id == target_id, Task.kind == kind,
                       Task.state.in_(["pending", "running"]))) if payload is None or t.payload == payload), None)
    if existing:
        return existing
    task = Task(kind=kind, target_id=target_id, actor_id=actor_id, payload=payload or {})
    db.add(task)
    db.commit()
    enqueue(db, task)
    return task


def enqueue(db, task):
    if settings().task_mode == "inline":
        run_task(task.id)
        db.refresh(task)
    else:
        try:
            run_task.delay(task.id)
        except Exception:
            task.state, task.error = "failed", "任务队列不可用，请检查 Redis 与 Worker 后重试"
            db.commit()
