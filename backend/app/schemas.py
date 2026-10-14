from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Credentials(Strict):
    username: str = Field(min_length=3, max_length=80, pattern=r"^[a-zA-Z0-9_@.\-]+$")
    password: str = Field(min_length=8, max_length=128)


class ProfileInput(Strict):
    name: str = Field(min_length=1, max_length=120)
    provider: Literal["openai", "ollama", "demo"]
    model: str = Field(min_length=1, max_length=150)
    base_url: str = ""
    api_key: str | None = None
    temperature: float = Field(default=0.1, ge=0, le=2)
    purpose: Literal["generation", "embedding"] = "generation"
    dimension: int | None = Field(default=None, ge=1, le=8192)

    @model_validator(mode="after")
    def embedding_dimension(self):
        if self.purpose == "embedding" and not self.dimension:
            raise ValueError("Embedding 配置需要向量维度")
        return self


class KBInput(Strict):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=3000)
    extraction_profile_id: str | None = None


class AgentOptions(Strict):
    # Old applications keep their original behavior until an administrator publishes this option.
    enabled: bool = False
    max_steps: int = Field(default=6, ge=1, le=12)
    max_subagents: int = Field(default=3, ge=0, le=5)
    max_parallel_subagents: int = Field(default=2, ge=1, le=5)
    max_tool_calls: int = Field(default=24, ge=1, le=48)
    timeout_seconds: int = Field(default=180, ge=15, le=600)
    skills: list[str] = Field(default_factory=lambda: [
        "course-evidence", "calculus-verification", "document-comparison",
        "sympy", "scientific-critical-thinking",
    ], max_length=20)


class ApplicationInput(Strict):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=3000)
    prompt: str = Field(default="你是一位严谨、耐心的课程学习助教。请依据当前课程资料回答问题。", max_length=5000)
    model_profile_id: str
    embedding_profile_id: str
    kb_ids: list[str] = Field(default_factory=list, max_length=20)
    features: list[Literal["chat", "quiz"]] = Field(default_factory=lambda: ["chat", "quiz"], min_length=1)
    agent: AgentOptions = Field(default_factory=AgentOptions)

    @model_validator(mode="before")
    @classmethod
    def retire_student_review(cls, value):
        if isinstance(value, dict) and isinstance(value.get("features"), list):
            return {**value, "features": [f for f in value["features"] if f != "review"]}
        return value


def student_features(features):
    """Project historical app configurations onto the current student-facing features."""
    return [f for f in features if f in {"chat", "quiz"}]


class SourceInput(Strict):
    document_id: str | None = None
    locator: str = Field(min_length=1, max_length=300)
    excerpt: str = Field(default="", max_length=20000)
    note: str = Field(default="", max_length=3000)
    page: int | None = Field(default=None, ge=1)
    bbox: list[float] | None = Field(default=None, min_length=4, max_length=4)
    asset_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]+\.png$")
    parser: str | None = None
    confidence: float | None = Field(default=None,ge=0,le=1)
    warnings: list[str] = Field(default_factory=list,max_length=30)


class SplitOptions(Strict):
    method: Literal["document", "page", "paragraph", "fixed", "recursive", "semantic"] = "paragraph"
    chunk_size: int = Field(default=1200, ge=100, le=24000)
    overlap: int = Field(default=0, ge=0, le=4000)
    model_profile_id: str | None = None

    @model_validator(mode="after")
    def bounds(self):
        if self.overlap >= self.chunk_size:
            raise ValueError("重叠长度必须小于切分长度")
        if self.overlap and self.method not in {"fixed", "recursive"}:
            raise ValueError("只有固定长度和递归切分支持重叠")
        return self


class ParseOptions(Strict):
    mode: Literal["auto","scan","text"] = "auto"
    page_start: int = Field(default=1,ge=1)
    page_end: int | None = Field(default=None,ge=1)
    splitting: SplitOptions = Field(default_factory=SplitOptions)

    @model_validator(mode="before")
    @classmethod
    def discard_legacy_review(cls, value):
        # Stored settings and older clients must never restart retired review jobs.
        return {k: v for k, v in value.items() if k != "auto_review"} if isinstance(value, dict) else value

    @model_validator(mode="after")
    def page_bounds(self):
        if self.page_end is not None and self.page_end < self.page_start:
            raise ValueError("结束页不能早于起始页")
        return self


class ContentInput(Strict):
    @model_validator(mode="before")
    @classmethod
    def discard_legacy_review(cls, value):
        return {k: v for k, v in value.items() if k != "reviewed"} if isinstance(value, dict) else value


class KnowledgeInput(ContentInput):
    kind: Literal["concept", "theorem", "formula", "example", "note"]
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1, max_length=30000)
    attributes: dict = Field(default_factory=dict)
    sources: list[SourceInput] = Field(min_length=1, max_length=256)
    change_note: str = Field(default="人工维护", max_length=1000)


class RelationInput(ContentInput):
    source_id: str
    target_id: str
    kind: Literal["prerequisite", "uses_theorem", "uses_formula", "assesses", "confusable", "related"]


class RelationGenerationInput(Strict):
    model_profile_id: str | None = None


class QuestionInput(ContentInput):
    title: str = Field(min_length=1, max_length=10000)
    kind: Literal["choice", "numeric", "expression", "antiderivative", "proof"] = "expression"
    answer: str = Field(min_length=1, max_length=10000)
    explanation: str = Field(default="", max_length=20000)
    knowledge_ids: list[str] = Field(min_length=1, max_length=20)
    attributes: dict = Field(default_factory=dict)


class MergeInput(Strict):
    canonical_id: str
    duplicate_ids: list[str] = Field(min_length=1, max_length=20)


class MathRequest(Strict):
    operation: Literal["differentiate", "integrate", "limit"]
    expression: str = Field(min_length=1, max_length=500)
    variable: Literal["x", "t"] = "x"
    point: str | None = Field(default=None, max_length=100)
    lower: str | None = Field(default=None, max_length=100)
    upper: str | None = Field(default=None, max_length=100)
    direction: Literal["+", "-", "+-"] = "+-"
    assumption: Literal["real", "positive", "negative", "nonnegative"] = "real"

    @model_validator(mode="after")
    def bounds(self):
        if (self.lower is None) != (self.upper is None):
            raise ValueError("定积分必须同时提供上下界")
        return self


class ChatInput(Strict):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = None
    calculation: MathRequest | None = None
    request_id: str | None = None

    @model_validator(mode="after")
    def normalize_request_id(self):
        if self.request_id is not None:
            try:
                self.request_id = str(UUID(self.request_id))
            except (ValueError, AttributeError) as exc:
                raise ValueError("request_id 必须是 UUID") from exc
        return self


class QuizInput(Strict):
    count: int = Field(default=20, ge=1, le=50)
    chapter: str | None = None
    difficulty: Literal["easy", "medium", "hard"] | None = None
    knowledge_ids: list[str] = Field(default_factory=list, max_length=20)


class AnswerInput(Strict):
    question_id: str
    answer: str = Field(min_length=1, max_length=2000)


class TemplateInput(Strict):
    question: QuestionInput
    parameters: dict[str, list[int]]
    count: int = Field(default=5, ge=1, le=30)
