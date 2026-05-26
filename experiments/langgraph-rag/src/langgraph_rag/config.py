"""Конфигурация эксперимента из .env через pydantic-settings."""

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Все параметры pipeline: Postgres, embeddings, LLM-роли, Confluence."""

    pg_dsn: str
    pg_collection: str = "langgraph_rag"

    embedding_model: str = "intfloat/multilingual-e5-large"
    embedding_device: str = "cpu"
    embedding_cache_dir: Path | None = None

    chunk_size: int = 1000
    chunk_overlap: int = 200

    top_k_vector: int = 10
    top_k_bm25: int = 10

    classifier_model: str
    keyword_extractor_model: str
    answerer_model: str

    litellm_api_base: str | None = None
    litellm_api_key: str | None = None

    confluence_url: str | None = None
    confluence_username: str | None = None
    confluence_api_token: str | None = None
    confluence_space_keys: list[str] = Field(default_factory=list)

    bm25_corpus_path: Path = Path("./data/bm25_corpus.pkl")

    debug_http: bool = False
    debug_sql: Literal["off", "info", "debug"] = "off"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="LGRAG_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    @classmethod
    def load(cls) -> "Settings":
        """
        Читает значения из env/.env; типы required-полей подтягиваются из окружения
        """
        return cls()  # type: ignore[call-arg]
