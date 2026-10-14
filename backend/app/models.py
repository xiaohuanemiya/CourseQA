import uuid
from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def uid():
    return str(uuid.uuid4())


def now():
    return datetime.now(timezone.utc)


class Identity:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class User(Identity, Base):
    __tablename__ = "users"
    username: Mapped[str] = mapped_column(String(80), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(20), default="student")


class LoginSession(Identity, Base):
    __tablename__ = "login_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ModelProfile(Identity, Base):
    __tablename__ = "model_profiles"
    name: Mapped[str] = mapped_column(String(120))
    provider: Mapped[str] = mapped_column(String(30))
    model: Mapped[str] = mapped_column(String(150))
    base_url: Mapped[str] = mapped_column(Text, default="")
    secret: Mapped[str] = mapped_column(Text, default="")
    temperature: Mapped[float] = mapped_column(Float, default=0.1)
    purpose: Mapped[str] = mapped_column(String(20), default="generation")
    dimension: Mapped[int | None] = mapped_column(Integer)


class KnowledgeBase(Identity, Base):
    __tablename__ = "knowledge_bases"
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    extraction_profile_id: Mapped[str | None] = mapped_column(ForeignKey("model_profiles.id"))
    current_release_id: Mapped[str | None] = mapped_column(String(36))
    revision: Mapped[int] = mapped_column(Integer, default=0)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)


class Application(Identity, Base):
    __tablename__ = "applications"
    draft: Mapped[dict] = mapped_column(JSON)
    published: Mapped[dict | None] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)


class AppRevision(Identity, Base):
    __tablename__ = "app_revisions"
    app_id: Mapped[str] = mapped_column(ForeignKey("applications.id"))
    version: Mapped[int] = mapped_column(Integer)
    configuration: Mapped[dict] = mapped_column(JSON)
    author_id: Mapped[str] = mapped_column(ForeignKey("users.id"))


class Document(Identity, Base):
    __tablename__ = "documents"
    kb_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    filename: Mapped[str] = mapped_column(Text)
    path: Mapped[str] = mapped_column(Text)
    checksum: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)
    withdrawn: Mapped[bool] = mapped_column(Boolean, default=False)
    state: Mapped[str] = mapped_column(String(20), default="uploaded")
    parse_options: Mapped[dict] = mapped_column(JSON, default=dict)
    recognition: Mapped[dict] = mapped_column(JSON, default=dict)


class Knowledge(Identity, Base):
    __tablename__ = "knowledge"
    kb_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(String(300))
    content: Mapped[str] = mapped_column(Text)
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    sources: Mapped[list] = mapped_column(JSON, default=list)
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False)
    withdrawn: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[int] = mapped_column(Integer, default=1)


class Relation(Identity, Base):
    __tablename__ = "relations"
    kb_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("knowledge.id"))
    target_id: Mapped[str] = mapped_column(ForeignKey("knowledge.id"))
    kind: Mapped[str] = mapped_column(String(40))
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False)
    withdrawn: Mapped[bool] = mapped_column(Boolean, default=False)


class Question(Identity, Base):
    __tablename__ = "questions"
    kb_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    title: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(30), default="expression")
    answer: Mapped[str] = mapped_column(Text)
    explanation: Mapped[str] = mapped_column(Text, default="")
    knowledge_ids: Mapped[list] = mapped_column(JSON, default=list)
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False)
    withdrawn: Mapped[bool] = mapped_column(Boolean, default=False)


class Release(Identity, Base):
    __tablename__ = "releases"
    kb_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict] = mapped_column(JSON)
    embedding_signature: Mapped[str] = mapped_column(Text)
    checksum: Mapped[str] = mapped_column(String(64))
    author_id: Mapped[str] = mapped_column(ForeignKey("users.id"))


class ReleaseVector(Identity, Base):
    __tablename__ = "release_vectors"
    release_id: Mapped[str] = mapped_column(ForeignKey("releases.id"), index=True)
    knowledge_id: Mapped[str] = mapped_column(String(36))
    signature: Mapped[str] = mapped_column(String(64), index=True)
    embedding: Mapped[list] = mapped_column(Vector().with_variant(JSON, "sqlite"))


class EmbeddingIndex(Identity, Base):
    __tablename__ = "embedding_indexes"
    __table_args__ = (UniqueConstraint("release_id", "signature"),)
    release_id: Mapped[str] = mapped_column(ForeignKey("releases.id"))
    profile_id: Mapped[str] = mapped_column(ForeignKey("model_profiles.id"))
    signature: Mapped[str] = mapped_column(String(64))
    dimension: Mapped[int] = mapped_column(Integer)
    count: Mapped[int] = mapped_column(Integer)


class Audit(Identity, Base):
    __tablename__ = "audit"
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    entity_id: Mapped[str] = mapped_column(String(36), index=True)
    action: Mapped[str] = mapped_column(String(50))
    before: Mapped[dict | None] = mapped_column(JSON)
    after: Mapped[dict | None] = mapped_column(JSON)


class Task(Identity, Base):
    __tablename__ = "tasks"
    kind: Mapped[str] = mapped_column(String(30))
    target_id: Mapped[str] = mapped_column(String(36))
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    state: Mapped[str] = mapped_column(String(20), default="pending")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict | None] = mapped_column(JSON)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class KnowledgeReview(Identity, Base):
    __tablename__ = "knowledge_reviews"
    kb_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    knowledge_id: Mapped[str] = mapped_column(ForeignKey("knowledge.id"), index=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    profile_id: Mapped[str] = mapped_column(ForeignKey("model_profiles.id"))
    input_revision: Mapped[int] = mapped_column(Integer)
    output_revision: Mapped[int] = mapped_column(Integer)
    fingerprint: Mapped[str] = mapped_column(String(64))
    profile_signature: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(30))
    applied: Mapped[bool] = mapped_column(Boolean, default=False)
    result: Mapped[dict] = mapped_column(JSON)
    evidence: Mapped[list] = mapped_column(JSON)


class Conversation(Identity, Base):
    __tablename__ = "conversations"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    app_id: Mapped[str] = mapped_column(ForeignKey("applications.id"), index=True)
    title: Mapped[str] = mapped_column(String(100))


class Message(Identity, Base):
    __tablename__ = "messages"
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    question: Mapped[str] = mapped_column(Text)
    result: Mapped[dict] = mapped_column(JSON)


class ChatRun(Identity, Base):
    __tablename__ = "chat_runs"
    __table_args__ = (UniqueConstraint("user_id", "app_id", "request_id", name="uq_chat_run_request"),)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    app_id: Mapped[str] = mapped_column(ForeignKey("applications.id"), index=True)
    request_id: Mapped[str] = mapped_column(String(36))
    payload_sha256: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    owner_token: Mapped[str] = mapped_column(String(36))
    lease_expires_at: Mapped[float] = mapped_column(Float, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    result: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[dict | None] = mapped_column(JSON)


class AdmissionLease(Base):
    __tablename__ = "admission_leases"
    scope: Mapped[str] = mapped_column(String(180), primary_key=True)
    slot: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_token: Mapped[str | None] = mapped_column(String(36), index=True)
    expires_at: Mapped[float] = mapped_column(Float, default=0, index=True)


class Quiz(Identity, Base):
    __tablename__ = "quizzes"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    app_id: Mapped[str] = mapped_column(ForeignKey("applications.id"), index=True)
    questions: Mapped[list] = mapped_column(JSON)
    versions: Mapped[dict] = mapped_column(JSON)


class Attempt(Identity, Base):
    __tablename__ = "attempts"
    __table_args__ = (UniqueConstraint("quiz_id", "question_id"),)
    quiz_id: Mapped[str] = mapped_column(ForeignKey("quizzes.id"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    app_id: Mapped[str] = mapped_column(ForeignKey("applications.id"), index=True)
    question_id: Mapped[str] = mapped_column(String(36))
    knowledge_ids: Mapped[list] = mapped_column(JSON)
    answer: Mapped[str] = mapped_column(Text)
    verdict: Mapped[str] = mapped_column(String(30))
    feedback: Mapped[dict] = mapped_column(JSON)
