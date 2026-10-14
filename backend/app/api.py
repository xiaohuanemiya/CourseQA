import hashlib
import itertools
import json
import re
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import session
from .models import (Application, Attempt, Audit, Conversation, Document, Knowledge, KnowledgeBase, KnowledgeReview, LoginSession,
                     Message, ModelProfile, Question, Quiz, Relation, Release, Task, User, uid)
from .schemas import (AnswerInput, ApplicationInput, ChatInput, Credentials, KBInput, KnowledgeInput,
                      MergeInput, ParseOptions, ProfileInput, QuestionInput, QuizInput, RelationInput, RelationGenerationInput, TemplateInput,
                      student_features)
from .security import admin, current_user, encrypt, new_session, password_hash, verify_password
from .services import knowledge as ks, learning
from .services.math_engine import bounded
from .services.providers import check_profile, discover_local
from .services.recognition import IMAGE_EXTENSIONS, asset_path, read_report
from .services.ingestion import resolve_split_profile
from .services.splitting import METHODS
from .tasks import dispatch

router = APIRouter(prefix="/api")
admin_router = APIRouter(prefix="/admin", dependencies=[Depends(admin)])


@admin_router.get("/environment")
def environment():
    return {"local_model_url": settings().embedding_base_url, "demo_enabled": settings().allow_demo_provider}


def get(db, cls, entity_id, kb_id=None):
    entity = db.get(cls, entity_id)
    if not entity or getattr(entity, "deleted", False) or (kb_id and entity.kb_id != kb_id):
        raise HTTPException(404, "记录不存在")
    return entity


def user_view(user):
    return {"id": user.id, "username": user.username, "role": user.role}


def cookie(response, token):
    response.set_cookie("calculus_session", token, httponly=True, secure=settings().cookie_secure,
                        samesite="lax", max_age=settings().session_hours*3600)


@router.post("/auth/register")
def register(data: Credentials, response: Response, db: Session = Depends(session)):
    if db.scalar(select(User).where(User.username == data.username)):
        raise HTTPException(409, "用户名已存在")
    user = User(username=data.username, password_hash=password_hash(data.password), role="student")
    db.add(user)
    db.commit()
    cookie(response, new_session(db, user))
    return user_view(user)


@router.post("/auth/login")
def login(data: Credentials, response: Response, db: Session = Depends(session)):
    user = db.scalar(select(User).where(User.username == data.username))
    if not user or not verify_password(data.password, user.password_hash):
        raise HTTPException(401, "用户名或密码错误")
    cookie(response, new_session(db, user))
    return user_view(user)


@router.post("/auth/logout")
def logout(request: Request, response: Response, db: Session = Depends(session)):
    record = db.scalar(select(LoginSession).where(LoginSession.token_hash == hashlib.sha256(
        request.cookies.get("calculus_session", "").encode()).hexdigest()))
    if record:
        db.delete(record)
        db.commit()
    response.delete_cookie("calculus_session")
    return {"ok": True}


@router.get("/auth/me")
def me(user=Depends(current_user)):
    return user_view(user)


@admin_router.get("/model-profiles")
def profiles(db: Session = Depends(session)):
    return [ks.serialize(p) | {"has_api_key": bool(p.secret)} for p in db.scalars(select(ModelProfile))]


@admin_router.post("/model-profiles")
def create_profile(data: ProfileInput, db: Session = Depends(session)):
    validate_profile(data)
    item = ModelProfile(**data.model_dump(exclude={"api_key"}), secret=encrypt(data.api_key or ""))
    db.add(item)
    db.commit()
    return ks.serialize(item) | {"has_api_key": bool(item.secret)}


def validate_profile(data):
    if data.provider == "demo" and not settings().allow_demo_provider:
        raise HTTPException(422, "演示模型未启用")
    if data.provider != "demo" and not re.match(r"^https?://[^\s]+$", data.base_url):
        raise HTTPException(422, "请填写完整 HTTP(S) 服务地址；云端兼容接口需要包含 /v1")


@admin_router.put("/model-profiles/{profile_id}")
def edit_profile(profile_id: str, data: ProfileInput, db: Session = Depends(session)):
    item = get(db, ModelProfile, profile_id)
    validate_profile(data)
    changed = any(getattr(item, k) != getattr(data, k) for k in ["provider", "model", "base_url", "dimension", "purpose"])
    if changed:
        for app in db.scalars(select(Application).where(Application.active, ~Application.deleted)):
            if profile_id in [app.published["model_profile_id"], app.published["embedding_profile_id"]]:
                raise HTTPException(409, "此配置用于已发布应用。请复制为新配置后在应用中切换，旧应用将继续工作")
    for k, v in data.model_dump(exclude={"api_key"}).items():
        setattr(item, k, v)
    if data.api_key is not None:
        item.secret = encrypt(data.api_key)
    db.commit()
    return ks.serialize(item) | {"has_api_key": bool(item.secret)}


@admin_router.delete("/model-profiles/{profile_id}")
def delete_profile(profile_id: str, db: Session = Depends(session)):
    item = get(db, ModelProfile, profile_id)
    for app in db.scalars(select(Application).where(~Application.deleted)):
        if any(profile_id in [cfg.get("model_profile_id"), cfg.get("embedding_profile_id")]
               for cfg in [app.draft, app.published or {}]):
            raise HTTPException(409, "模型配置仍被应用引用")
    if db.scalar(select(KnowledgeBase).where(KnowledgeBase.extraction_profile_id == profile_id)):
        raise HTTPException(409, "模型配置仍被知识库引用")
    if db.scalar(select(KnowledgeReview).where(KnowledgeReview.profile_id == profile_id)):
        raise HTTPException(409, "模型配置仍有关联的自动审核记录，请保留以便追溯")
    for doc in db.scalars(select(Document).where(~Document.withdrawn)):
        if (doc.parse_options or {}).get("splitting", {}).get("model_profile_id") == profile_id:
            raise HTTPException(409, "模型配置仍被文档切分设置引用")
    # Retain referenced historical index metadata; users can reuse profiles safely.
    from .models import EmbeddingIndex
    if db.scalar(select(EmbeddingIndex).where(EmbeddingIndex.profile_id == profile_id)):
        raise HTTPException(409, "模型配置仍有已构建索引，请保留以便追溯")
    db.delete(item)
    db.commit()
    return {"ok": True}


@admin_router.post("/model-profiles/{profile_id}/test")
def test_profile(profile_id: str, db: Session = Depends(session)):
    return check_profile(get(db, ModelProfile, profile_id))


@admin_router.get("/models/discover")
def discover(base_url: str = "http://127.0.0.1:11434"):
    if not re.match(r"^https?://[^\s]+$", base_url):
        raise HTTPException(422, "无效服务地址")
    return discover_local(base_url)


@admin_router.get("/apps")
def admin_apps(db: Session = Depends(session)):
    result = []
    for app in db.scalars(select(Application).where(~Application.deleted)):
        item = ks.serialize(app)
        for key in ("draft", "published"):
            if item[key]:
                item[key] = {**item[key], "features": student_features(item[key].get("features", []))}
                snapshot = item[key].pop("skill_snapshot", None)
                if snapshot is not None:
                    from .services.skills import snapshot_info
                    item[key]["skill_version"] = snapshot_info(snapshot)
        result.append(item)
    return result


@admin_router.get("/agent-capabilities")
def agent_capabilities():
    from .schemas import AgentOptions
    from .services.agents import TOOLS
    from .services.skills import catalog
    return {"skills": catalog(), "tools": [{"name": name, "description": spec[1]} for name, spec in TOOLS.items()],
            "defaults": AgentOptions().model_dump()}


def app_learners(db, app_id, administrator):
    learning.application(db, app_id)
    ids = {administrator.id}
    for model in (Attempt, Quiz, Conversation):
        ids.update(db.scalars(select(model.user_id).where(model.app_id == app_id).distinct()))
    return db.scalars(select(User).where(User.id.in_(ids)).order_by(User.username)).all()


@admin_router.get("/apps/{app_id}/learners")
def learners(app_id: str, db: Session = Depends(session), user=Depends(admin)):
    return [{"id": learner.id, "username": learner.username} for learner in app_learners(db, app_id, user)]


@admin_router.get("/apps/{app_id}/review-plan")
def admin_review_plan(app_id: str, user_id: str | None = None, db: Session = Depends(session), user=Depends(admin)):
    target = next((u for u in app_learners(db, app_id, user) if u.id == (user_id or user.id)), None)
    if not target:
        raise HTTPException(404, "此用户没有本应用的学习记录")
    return {**learning.review_plan(db, app_id, target), "learner": {"id": target.id, "username": target.username}}


def validate_app(db, data):
    from .services.skills import catalog
    available = {s["name"]: s for s in catalog()}
    if not set(data.agent.skills) <= set(available):
        raise HTTPException(422, "应用配置包含不可用的 Skill")
    for name in data.agent.skills:
        skill = available[name]
        if skill["compatibility"]["status"] == "incompatible":
            missing = ", ".join(skill["compatibility"]["missing_tools"])
            raise HTTPException(422, f"Skill {name} 不兼容：" + (f"缺少工具 {missing}" if missing else skill["runtime_note"]))
    for key, purpose in [(data.model_profile_id, "generation"), (data.embedding_profile_id, "embedding")]:
        if get(db, ModelProfile, key).purpose != purpose:
            raise HTTPException(422, "模型用途不匹配")
    for kb_id in data.kb_ids:
        ks.kb_get(db, kb_id)


@admin_router.post("/apps")
def add_app(data: ApplicationInput, db: Session = Depends(session)):
    validate_app(db, data)
    item = Application(draft=data.model_dump())
    db.add(item)
    db.commit()
    return ks.serialize(item)


@admin_router.put("/apps/{app_id}")
def edit_app(app_id: str, data: ApplicationInput, db: Session = Depends(session)):
    item = get(db, Application, app_id)
    validate_app(db, data)
    item.draft = data.model_dump()
    item.version += 1
    db.commit()
    return ks.serialize(item)


@admin_router.post("/apps/{app_id}/publish")
def publish_app(app_id: str, db: Session = Depends(session), user=Depends(admin)):
    get(db, Application, app_id)
    return ks.serialize(dispatch(db, "publish_app", app_id, user.id))


@admin_router.post("/apps/{app_id}/unpublish")
def unpublish_app(app_id: str, db: Session = Depends(session)):
    item = get(db, Application, app_id)
    item.active = False
    item.version += 1
    db.commit()
    return {"ok": True}


@admin_router.delete("/apps/{app_id}")
def delete_app(app_id: str, db: Session = Depends(session)):
    item = get(db, Application, app_id)
    item.active, item.deleted = False, True
    item.version += 1
    db.commit()
    return {"ok": True}


@admin_router.get("/knowledge-bases")
def kbs(db: Session = Depends(session)):
    return [ks.serialize(k) for k in db.scalars(select(KnowledgeBase).where(~KnowledgeBase.deleted))]


def validate_kb(db, data):
    if data.extraction_profile_id and get(db, ModelProfile, data.extraction_profile_id).purpose != "generation":
        raise HTTPException(422, "抽取需要生成模型")


@admin_router.post("/knowledge-bases")
def create_kb(data: KBInput, db: Session = Depends(session)):
    validate_kb(db, data)
    kb = KnowledgeBase(**data.model_dump())
    db.add(kb)
    db.commit()
    return ks.serialize(kb)


@admin_router.put("/knowledge-bases/{kb_id}")
def edit_kb(kb_id: str, data: KBInput, db: Session = Depends(session)):
    kb = ks.kb_get(db, kb_id)
    validate_kb(db, data)
    for key, value in data.model_dump().items():
        setattr(kb, key, value)
    ks.touch(db, kb_id)
    db.commit()
    return ks.serialize(kb)


@admin_router.delete("/knowledge-bases/{kb_id}")
def delete_kb(kb_id: str, db: Session = Depends(session)):
    kb = ks.kb_get(db, kb_id)
    for app in db.scalars(select(Application).where(~Application.deleted)):
        if kb_id in app.draft["kb_ids"] or kb_id in (app.published or {}).get("kb_ids", []):
            raise HTTPException(409, "知识库仍被应用引用，请先解除绑定并发布应用配置")
    kb.deleted = True
    ks.touch(db, kb_id)
    db.commit()
    return {"ok": True}


@admin_router.get("/knowledge-bases/{kb_id}/contents")
def contents(kb_id: str, db: Session = Depends(session)):
    kb = ks.kb_get(db, kb_id)
    return {"kb": ks.serialize(kb), **{name: [ks.serialize(x) for x in db.scalars(select(cls).where(cls.kb_id == kb_id))]
            for name, cls in [("knowledge", Knowledge), ("relations", Relation), ("questions", Question),
                              ("documents", Document), ("releases", Release)]}}


@admin_router.post("/knowledge-bases/{kb_id}/documents")
def upload(kb_id: str, file: UploadFile = File(...), mode: str = Form("auto"),
           page_start: int = Form(1), page_end: int | None = Form(None),
           splitting: str = Form("{}"),
           db: Session = Depends(session), user=Depends(admin)):
    kb = ks.kb_get(db, kb_id)
    try:
        parsed=ParseOptions(mode=mode,page_start=page_start,page_end=page_end,splitting=json.loads(splitting))
        resolve_split_profile(db, parsed, kb)
        options=parsed.model_dump()
    except ValueError as exc:
        raise HTTPException(422,"识别、切分参数或生成模型配置无效") from exc
    name = Path(file.filename or "document").name
    if Path(name).suffix.lower() not in {".pdf", ".docx", ".pptx", ".txt", ".md"} | IMAGE_EXTENSIONS:
        raise HTTPException(422, "不支持此格式")
    if mode=="text" and Path(name).suffix.lower() in IMAGE_EXTENSIONS:
        raise HTTPException(422,"图片需要 PaddleOCR 图文识别")
    folder = settings().storage_dir / "documents"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (uid() + Path(name).suffix.lower())
    digest, total = hashlib.sha256(), 0
    try:
        with path.open("wb") as output:
            while chunk := file.file.read(1024*1024):
                total += len(chunk)
                if total > settings().max_upload_mb*1024*1024:
                    raise HTTPException(413, "文件超过大小限制")
                output.write(chunk)
                digest.update(chunk)
        if not total:
            raise HTTPException(422, "文件为空")
        existing = db.scalar(select(Document).where(Document.kb_id == kb_id, Document.checksum == digest.hexdigest(), ~Document.withdrawn))
        if existing:
            raise HTTPException(409, "相同文件已经导入")
        doc = Document(kb_id=kb_id, filename=name, path=str(path.resolve()), checksum=digest.hexdigest(),parse_options=options)
        db.add(doc)
        ks.touch(db, kb_id)
        db.commit()
    except Exception:
        path.unlink(missing_ok=True)
        raise
    task = dispatch(db, "ingest", doc.id, user.id)
    return {"document": ks.serialize(doc), "task": ks.serialize(task)}


@admin_router.get("/documents/{document_id}/file")
def admin_file(document_id: str, db: Session = Depends(session)):
    doc = get(db, Document, document_id)
    return FileResponse(doc.path, filename=doc.filename, content_disposition_type="inline")


@admin_router.post("/knowledge-bases/{kb_id}/documents/{document_id}/reprocess")
def reprocess_document(kb_id: str, document_id: str, options: ParseOptions | None = None,
                       db: Session = Depends(session), user=Depends(admin)):
    original = get(db, Document, document_id, kb_id)
    kb = ks.kb_get(db, kb_id)
    options = options or ParseOptions.model_validate(original.parse_options or {})
    try:
        resolve_split_profile(db, options, kb)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    doc = Document(kb_id=kb_id, filename=original.filename, path=original.path,
                   checksum=original.checksum, version=original.version+1,
                   parse_options=options.model_dump())
    db.add(doc)
    db.flush()
    ks.audit(db, user.id, doc.id, "reprocess", {"document_id": original.id}, ks.serialize(doc))
    ks.touch(db, kb_id)
    db.commit()
    return ks.serialize(dispatch(db, "ingest", doc.id, user.id))


@admin_router.get("/documents/{document_id}/recognition")
def recognition_report(document_id: str, db: Session = Depends(session)):
    get(db,Document,document_id)
    return read_report(document_id)


@admin_router.get("/splitting-methods")
def splitting_methods():
    return METHODS


@admin_router.get("/documents/{document_id}/splitting")
def splitting_report(document_id: str, db: Session = Depends(session)):
    doc = get(db, Document, document_id)
    return {"options": doc.parse_options, "report": (doc.recognition or {}).get("splitting")}


@admin_router.get("/documents/{document_id}/assets/{asset_id}")
def recognition_asset(document_id: str,asset_id: str,db: Session = Depends(session)):
    get(db,Document,document_id)
    return FileResponse(asset_path(document_id,asset_id),media_type="image/png",headers={"Cache-Control":"no-store"})


@admin_router.delete("/knowledge-bases/{kb_id}/documents/{document_id}")
def withdraw_document(kb_id: str, document_id: str, db: Session = Depends(session), user=Depends(admin)):
    doc = get(db, Document, document_id, kb_id)
    before = ks.serialize(doc)
    doc.withdrawn = True
    ids = []
    for n in ks.active_items(db, kb_id):
        if any(s.get("document_id") == doc.id for s in n.sources):
            n.withdrawn = True
            ids.append(n.id)
    for cls in [Relation, Question]:
        for entity in db.scalars(select(cls).where(cls.kb_id == kb_id)):
            if (isinstance(entity, Relation) and (entity.source_id in ids or entity.target_id in ids)) or (
                isinstance(entity, Question) and set(entity.knowledge_ids) & set(ids)):
                entity.withdrawn = True
    ks.audit(db, user.id, doc.id, "withdraw_document", before, ks.serialize(doc))
    ks.touch(db, kb_id)
    db.commit()
    return {"ok": True}


def save_knowledge(db, kb_id, data, user, item=None):
    ks.kb_get(db, kb_id)
    payload = data.model_dump(exclude={"change_note"})
    ks.validate_sources(db, kb_id, payload["sources"])
    before = ks.serialize(item) if item else None
    if not item:
        item = Knowledge(kb_id=kb_id, **payload)
        db.add(item)
    else:
        for key, value in payload.items():
            setattr(item, key, value)
        item.revision += 1
    db.flush()
    ks.audit(db, user.id, item.id, data.change_note, before, ks.serialize(item))
    ks.touch(db, kb_id)
    db.commit()
    return ks.serialize(item)


@admin_router.post("/knowledge-bases/{kb_id}/knowledge")
def add_knowledge(kb_id: str, data: KnowledgeInput, db: Session = Depends(session), user=Depends(admin)):
    return save_knowledge(db, kb_id, data, user)


@admin_router.put("/knowledge-bases/{kb_id}/knowledge/{item_id}")
def edit_knowledge(kb_id: str, item_id: str, data: KnowledgeInput, db: Session = Depends(session), user=Depends(admin)):
    return save_knowledge(db, kb_id, data, user, get(db, Knowledge, item_id, kb_id))


@admin_router.post("/knowledge-bases/{kb_id}/auto-review")
def auto_review_knowledge(kb_id: str, db: Session = Depends(session)):
    ks.kb_get(db, kb_id)
    raise HTTPException(410, "审核流程已移除，内容保存后可直接发布")


@admin_router.get("/knowledge-bases/{kb_id}/auto-reviews")
def auto_review_overview(kb_id: str, db: Session = Depends(session)):
    from collections import Counter
    ks.kb_get(db, kb_id)
    nodes = {n.id: n for n in ks.active_items(db, kb_id)}
    latest = {}
    for review in db.scalars(select(KnowledgeReview).where(KnowledgeReview.kb_id == kb_id).order_by(KnowledgeReview.created_at.desc())):
        if review.knowledge_id in latest or review.knowledge_id not in nodes:
            continue
        node = nodes[review.knowledge_id]
        decision = review.decision if node.revision == review.output_revision else "stale"
        latest[node.id] = {"id": review.id, "knowledge_id": node.id, "decision": decision,
            "applied": review.applied and decision == "approved" and node.reviewed,
            "model_name": review.result.get("model_name"), "reason": review.result.get("reason"),
            "mode": review.result.get("mode"), "created_at": review.created_at.isoformat()}
    return {"items": list(latest.values()), "counts": dict(Counter(r["decision"] for r in latest.values())),
            "total": len(nodes), "unreviewed": sum(not n.reviewed for n in nodes.values()),
            "not_evaluated": len(nodes)-len(latest)}


@admin_router.get("/knowledge-bases/{kb_id}/knowledge/{item_id}/reviews")
def knowledge_reviews(kb_id: str, item_id: str, db: Session = Depends(session)):
    get(db, Knowledge, item_id, kb_id)
    return [{**ks.serialize(r), "created_at": r.created_at.isoformat()} for r in db.scalars(
        select(KnowledgeReview).where(KnowledgeReview.knowledge_id == item_id).order_by(KnowledgeReview.created_at.desc()))]


@admin_router.get("/knowledge-bases/{kb_id}/duplicates")
def duplicates(kb_id: str, db: Session = Depends(session)):
    from difflib import SequenceMatcher
    items = ks.active_items(db, kb_id)
    if settings().semantica_enabled:
        from semantica.deduplication import detect_duplicates
        records = [{"id": n.id, "name": n.title, "type": n.kind, "aliases": n.attributes.get("aliases", [])} for n in items]
        found = detect_duplicates(records, similarity_threshold=.65, confidence_threshold=.4)
        return [{"a": c.entity1["id"], "b": c.entity2["id"], "title_a": c.entity1["name"],
                 "title_b": c.entity2["name"], "score": c.similarity_score}
                for c in found if c.entity1["type"] == c.entity2["type"]]
    candidates = []
    for a, b in itertools.combinations(items, 2):
        if a.kind != b.kind:
            continue
        score = SequenceMatcher(None, a.title, b.title).ratio()
        aliases = set(a.attributes.get("aliases", [])) & ({b.title} | set(b.attributes.get("aliases", [])))
        if score >= .65 or aliases:
            candidates.append({"a": a.id, "b": b.id, "title_a": a.title, "title_b": b.title,
                               "score": score})
    return candidates


@admin_router.post("/knowledge-bases/{kb_id}/merge")
def merge(kb_id: str, data: MergeInput, db: Session = Depends(session), user=Depends(admin)):
    canonical = get(db, Knowledge, data.canonical_id, kb_id)
    if canonical.id in data.duplicate_ids:
        raise HTTPException(422, "合并对象不能包含主知识点")
    before = ks.serialize(canonical)
    aliases = set(canonical.attributes.get("aliases", []))
    sources = list(canonical.sources)
    for key in data.duplicate_ids:
        duplicate = get(db, Knowledge, key, kb_id)
        aliases.add(duplicate.title)
        sources.extend(s for s in duplicate.sources if s not in sources)
        duplicate.withdrawn = True
    canonical.attributes = {**canonical.attributes, "aliases": sorted(aliases)}
    canonical.sources = sources
    canonical.revision += 1
    for edge in db.scalars(select(Relation).where(Relation.kb_id == kb_id, ~Relation.withdrawn)):
        if edge.source_id in data.duplicate_ids:
            edge.source_id = canonical.id
        if edge.target_id in data.duplicate_ids:
            edge.target_id = canonical.id
        if edge.source_id == edge.target_id:
            edge.withdrawn = True
    for q in db.scalars(select(Question).where(Question.kb_id == kb_id, ~Question.withdrawn)):
        q.knowledge_ids = list(dict.fromkeys(canonical.id if k in data.duplicate_ids else k for k in q.knowledge_ids))
    ks.audit(db, user.id, canonical.id, "merge", before, ks.serialize(canonical))
    ks.touch(db, kb_id)
    db.commit()
    return ks.serialize(canonical)


@admin_router.post("/knowledge-bases/{kb_id}/relations")
@admin_router.put("/knowledge-bases/{kb_id}/relations/{item_id}")
def save_relation(kb_id: str, data: RelationInput, item_id: str | None = None,
                  db: Session = Depends(session), user=Depends(admin)):
    for key in [data.source_id, data.target_id]:
        if get(db, Knowledge, key, kb_id).withdrawn:
            raise HTTPException(422, "关系引用了已撤回知识")
    if data.source_id == data.target_id:
        raise HTTPException(422, "不能建立自身关系")
    return save_entity(db, Relation, kb_id, data, item_id, user)


def save_entity(db, cls, kb_id, data, item_id, user):
    ks.kb_get(db, kb_id)
    item = get(db, cls, item_id, kb_id) if item_id else cls(kb_id=kb_id)
    before = ks.serialize(item) if item_id else None
    for key, value in data.model_dump().items():
        setattr(item, key, value)
    if cls is Relation and item_id:
        # The model's evidence applies to the original edge only; audit keeps its history.
        item.attributes = {}
    db.add(item)
    db.flush()
    ks.audit(db, user.id, item.id, "edit", before, ks.serialize(item))
    ks.touch(db, kb_id)
    db.commit()
    return ks.serialize(item)


@admin_router.get("/knowledge-bases/{kb_id}/relation-generation")
def relation_generation_status(kb_id: str, db: Session = Depends(session)):
    ks.kb_get(db, kb_id)
    tasks = select(Task).where(Task.target_id == kb_id, Task.kind == "generate_relations")
    active = db.scalar(tasks.where(Task.state.in_(["pending", "running"])).order_by(Task.created_at.desc()))
    latest = active or db.scalar(tasks.order_by(Task.created_at.desc()))
    return ks.serialize(latest) if latest else None


@admin_router.post("/knowledge-bases/{kb_id}/relation-generation")
def start_relation_generation(kb_id: str, data: RelationGenerationInput,
                              db: Session = Depends(session), user=Depends(admin)):
    from .services.relation_generation import resolve_profile
    kb = ks.kb_get(db, kb_id)
    try:
        profile = resolve_profile(db, kb, data.model_profile_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if len(ks.active_items(db, kb_id)) < 2:
        raise HTTPException(422, "至少需要两条有效知识才能生成关系")
    active = db.scalar(select(Task).where(Task.target_id == kb_id, Task.kind == "generate_relations",
                                         Task.state.in_(["pending", "running"])))
    if active:
        return ks.serialize(active)
    return ks.serialize(dispatch(db, "generate_relations", kb_id, user.id,
                                 {"model_profile_id": profile.id, "cache_key": uid()}))


@admin_router.post("/knowledge-bases/{kb_id}/questions")
@admin_router.put("/knowledge-bases/{kb_id}/questions/{item_id}")
def save_question(kb_id: str, data: QuestionInput, item_id: str | None = None,
                  db: Session = Depends(session), user=Depends(admin)):
    for key in data.knowledge_ids:
        if get(db, Knowledge, key, kb_id).withdrawn:
            raise HTTPException(422, "题目引用了已撤回知识")
    if data.kind not in {"choice", "proof"}:
        result = bounded("grade", (data.model_dump(), data.answer))
        if result["verdict"] != "correct":
            raise HTTPException(422, "参考答案未通过数学验证：" + result.get("reason", "无法判定"))
    return save_entity(db, Question, kb_id, data, item_id, user)


@admin_router.post("/knowledge-bases/{kb_id}/templates/generate")
def generate_template(kb_id: str, data: TemplateInput, db: Session = Depends(session), user=Depends(admin)):
    for key in data.question.knowledge_ids:
        get(db, Knowledge, key, kb_id)
    if not data.parameters or len(data.parameters) > 4 or any(not re.fullmatch(r"[a-z]", k) for k in data.parameters):
        raise HTTPException(422, "参数需为最多四个单字母名称")
    if any(not values or len(values) > 20 or any(abs(v) > 100 for v in values) for values in data.parameters.values()):
        raise HTTPException(422, "每个参数支持 1–20 个范围在 -100 到 100 的整数")
    created = []
    for combo in itertools.islice(itertools.product(*data.parameters.values()), data.count):
        variables = dict(zip(data.parameters, combo, strict=True))
        q = data.question.model_dump()
        for field in ["title", "answer", "explanation"]:
            for name, number in variables.items():
                q[field] = q[field].replace("{{" + name + "}}", str(number))
        if q["kind"] not in {"choice", "proof"}:
            result = bounded("grade", (q, q["answer"]))
            if result["verdict"] != "correct":
                raise HTTPException(422, "模板参考答案无效：" + result.get("reason", "无法判定"))
        q["attributes"] = {**q["attributes"], "generated": True, "parameters": variables}
        item = Question(kb_id=kb_id, **q)
        db.add(item)
        db.flush()
        created.append(item.id)
    ks.touch(db, kb_id)
    db.commit()
    return {"ids": created, "count": len(created), "message": "变式题已生成，可直接发布"}


@admin_router.delete("/knowledge-bases/{kb_id}/{resource}/{item_id}")
def withdraw(kb_id: str, resource: str, item_id: str, db: Session = Depends(session), user=Depends(admin)):
    cls = {"knowledge": Knowledge, "relations": Relation, "questions": Question}.get(resource)
    if not cls:
        raise HTTPException(404, "不存在此资源")
    item = get(db, cls, item_id, kb_id)
    before = ks.serialize(item)
    item.withdrawn = True
    if cls is Knowledge:
        for edge in db.scalars(select(Relation).where(Relation.kb_id == kb_id)):
            if item_id in [edge.source_id, edge.target_id]:
                edge.withdrawn = True
        for q in db.scalars(select(Question).where(Question.kb_id == kb_id)):
            if item_id in q.knowledge_ids:
                q.withdrawn = True
    ks.audit(db, user.id, item.id, "withdraw", before, ks.serialize(item))
    ks.touch(db, kb_id)
    db.commit()
    return {"ok": True}


@admin_router.post("/knowledge-bases/{kb_id}/publish")
def publish_kb(kb_id: str, db: Session = Depends(session), user=Depends(admin)):
    ks.kb_get(db, kb_id)
    return ks.serialize(dispatch(db, "publish", kb_id, user.id))


@admin_router.get("/audit/{entity_id}")
def history(entity_id: str, db: Session = Depends(session)):
    return [ks.serialize(a) for a in db.scalars(select(Audit).where(Audit.entity_id == entity_id).order_by(Audit.created_at.desc()))]


@admin_router.get("/tasks")
def tasks(include_archived: bool = False, db: Session = Depends(session)):
    query = select(Task)
    if not include_archived:
        query = query.where(Task.kind != "auto_review")
    return [ks.serialize(t) for t in db.scalars(query.order_by(Task.created_at.desc()).limit(100))]


@admin_router.get("/tasks/{task_id}")
def task(task_id: str, db: Session = Depends(session)):
    return ks.serialize(get(db, Task, task_id))


@admin_router.post("/tasks/{task_id}/retry")
def retry(task_id: str, db: Session = Depends(session), user=Depends(admin)):
    item = get(db, Task, task_id)
    if item.kind == "auto_review":
        raise HTTPException(410, "审核流程已移除，历史审核任务不再重试")
    if item.state != "failed":
        raise HTTPException(409, "只能重试失败任务")
    return ks.serialize(dispatch(db, item.kind, item.target_id, user.id, payload=item.payload))


@router.get("/apps")
def public_apps(db: Session = Depends(session)):
    return [public_app(a) for a in db.scalars(select(Application).where(Application.active, ~Application.deleted))]


def public_app(a):
    return {"id": a.id, "name": a.published["name"], "description": a.published["description"],
            "features": student_features(a.published["features"]), "knowledge_bases": len(a.published["kb_ids"]), "version": a.version}


@router.get("/apps/{app_id}")
def public_detail(app_id: str, db: Session = Depends(session)):
    return public_app(learning.application(db, app_id))


@router.post("/apps/{app_id}/chat")
def chat(app_id: str, data: ChatInput, db: Session = Depends(session), user=Depends(current_user)):
    # Authentication uses this read-only session; execution owns a separate one.
    db.commit()
    return learning.chat(db, app_id, user, data)


@router.post("/apps/{app_id}/chat/stream")
def chat_stream(app_id: str, data: ChatInput, db: Session = Depends(session), user=Depends(current_user)):
    from .services.streaming import chat_events, event_stream
    # Reject unauthorized scopes and conversations before sending SSE response headers.
    learning.scope(db, app_id, "chat")
    learning.chat_conversation(db, app_id, user.id, data.conversation_id)
    user_id = user.id
    db.commit()
    return StreamingResponse(event_stream(chat_events(app_id, user_id, data)), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.get("/apps/{app_id}/chat/requests/{request_id}")
def chat_request(app_id: str, request_id: str, db: Session = Depends(session), user=Depends(current_user)):
    from .services.chat_runs import get_run
    return get_run(db, app_id, user.id, request_id)


@router.post("/apps/{app_id}/chat/requests/{request_id}/cancel")
def cancel_chat_request(app_id: str, request_id: str, db: Session = Depends(session), user=Depends(current_user)):
    from .services.chat_runs import cancel_run
    return cancel_run(db, app_id, user.id, request_id)


@router.get("/apps/{app_id}/study-options")
def study_options(app_id: str, db: Session = Depends(session), user=Depends(current_user)):
    app, releases = learning.scope(db, app_id)
    nodes = [n for _, snap in releases for n in snap["nodes"]]
    quiz_enabled = "quiz" in student_features(app.published["features"])
    return {"suggestions": [n["title"] for n in nodes[:3]],
            "knowledge": [{"id": n["id"], "title": n["title"]} for n in nodes] if quiz_enabled else [],
            "chapters": sorted({n["attributes"]["chapter"] for n in nodes if n["attributes"].get("chapter")}) if quiz_enabled else []}


@admin_router.get("/apps/{app_id}/graph")
@router.get("/apps/{app_id}/graph", deprecated=True)
def graph(app_id: str, db: Session = Depends(session), user=Depends(admin)):
    _, releases = learning.scope(db, app_id)
    return {"nodes": [{"id": n["id"], "title": n["title"], "kind": n["kind"], "attributes": n["attributes"]}
                       for _, snap in releases for n in snap["nodes"]],
            "edges": [e for _, snap in releases for e in snap["edges"]]}


@router.get("/apps/{app_id}/sources/{source_id}")
def source(app_id: str, source_id: str, release_id: str | None = None, db: Session = Depends(session), user=Depends(current_user)):
    _, releases = learning.scope(db, app_id)
    for release, snap in releases:
        for n in snap["nodes"]:
            if n["id"] == source_id:
                if release_id and release.id != release_id:
                    raise HTTPException(409, "引用对应的资料版本已更新，请重新提问以获取当前依据")
                return {**n, "release_id": release.id}
    raise HTTPException(404, "来源不属于此应用或已撤回")


@router.get("/apps/{app_id}/documents/{document_id}")
def source_file(app_id: str, document_id: str, db: Session = Depends(session), user=Depends(current_user)):
    _, releases = learning.scope(db, app_id)
    allowed = {s["document_id"] for _, snap in releases for n in snap["nodes"] for s in n["sources"] if s.get("document_id")}
    if document_id not in allowed:
        raise HTTPException(404, "文档不属于此应用或已撤回")
    doc = get(db, Document, document_id)
    return FileResponse(doc.path, filename=doc.filename, content_disposition_type="inline")


@router.get("/apps/{app_id}/documents/{document_id}/assets/{asset_id}")
def source_asset(app_id: str,document_id: str,asset_id: str,db: Session = Depends(session),user=Depends(current_user)):
    _,releases=learning.scope(db,app_id)
    allowed={s["document_id"] for _,snap in releases for n in snap["nodes"] for s in n["sources"] if s.get("document_id")}
    if document_id not in allowed:
        raise HTTPException(404,"图片来源不属于此应用或已撤回")
    return FileResponse(asset_path(document_id,asset_id),media_type="image/png",headers={"Cache-Control":"no-store"})


@router.get("/apps/{app_id}/conversations")
def conversations(app_id: str, db: Session = Depends(session), user=Depends(current_user)):
    learning.application(db, app_id, "chat")
    return [ks.serialize(c) for c in db.scalars(select(Conversation).where(Conversation.app_id == app_id,
            Conversation.user_id == user.id).order_by(Conversation.created_at.desc()))]


@router.get("/apps/{app_id}/conversations/{conversation_id}")
def messages(app_id: str, conversation_id: str, db: Session = Depends(session), user=Depends(current_user)):
    _, releases = learning.scope(db, app_id, "chat")
    c = get(db, Conversation, conversation_id)
    if c.user_id != user.id or c.app_id != app_id:
        raise HTTPException(404, "会话不存在")
    valid = {n["id"] for _, snap in releases for n in snap["nodes"]}
    output = []
    for m in db.scalars(select(Message).where(Message.conversation_id == c.id).order_by(Message.created_at)):
        if all(c["knowledge_id"] in valid for c in m.result["citations"]) and m.result["versions"] == learning.versions(releases):
            output.append(ks.serialize(m))
        else:
            output.append({"id": m.id, "question": m.question, "result": {"status": "outdated", "answer": "此历史回答依据的资料已更新或撤回，请重新提问。", "citations": []}})
    return output


@router.post("/apps/{app_id}/quizzes")
def quiz(app_id: str, data: QuizInput, db: Session = Depends(session), user=Depends(current_user)):
    return learning.create_quiz(db, app_id, user, data)


@router.post("/apps/{app_id}/quizzes/{quiz_id}/answers")
def answer(app_id: str, quiz_id: str, data: AnswerInput, db: Session = Depends(session), user=Depends(current_user)):
    return learning.submit(db, app_id, quiz_id, user, data)


@router.get("/apps/{app_id}/review-plan", deprecated=True)
def review(app_id: str, db: Session = Depends(session), user=Depends(admin)):
    return learning.review_plan(db, app_id, user)


router.include_router(admin_router)
