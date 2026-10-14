from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "sqlite:///./data/calculus.db"
    secret_key: str = ""
    storage_dir: Path = Path("data")
    task_mode: str = "celery"
    redis_url: str = "redis://localhost:6379/0"
    semantica_enabled: bool = True
    allow_demo_provider: bool = False
    embedding_provider: str = "ollama"
    embedding_base_url: str = "http://localhost:11434"
    embedding_model: str = "bge-m3"
    embedding_dimension: int = 1024
    embedding_api_key: str = ""
    model_timeout: float = 90
    model_concurrency: int = Field(default=4, ge=1, le=64)
    model_queue_capacity: int = Field(default=24, ge=0, le=1000)
    model_queue_timeout: float = Field(default=30, gt=0, le=600)
    user_chat_concurrency: int = Field(default=2, ge=1, le=20)
    user_chat_queue_capacity: int = Field(default=4, ge=0, le=100)
    user_chat_queue_timeout: float = Field(default=30, gt=0, le=600)
    admission_lease_seconds: float = Field(default=30, ge=3, le=600)
    admission_poll_seconds: float = Field(default=.2, ge=.01, le=5)
    cookie_secure: bool = False
    max_upload_mb: int = 30
    session_hours: int = 24
    ocr_enabled: bool = True
    ocr_timeout: float = 3600
    paddleocr_token: str = ""
    paddleocr_job_url: str = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
    paddleocr_model: str = "PaddleOCR-VL-1.6"
    paddleocr_poll_interval: float = 5

    @property
    def embedding_signature(self) -> str:
        return f"{self.embedding_provider}:{self.embedding_model}:{self.embedding_dimension}"


@lru_cache
def settings() -> Settings:
    return Settings()
