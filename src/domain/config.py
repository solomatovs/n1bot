"""Конфигурация приложения."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class AppConfig:
    """Единственный источник конфигурации приложения.

    Все секреты и переменные окружения читаются здесь.
    Остальные модули получают значения через cfg.*.
    """
    chroma_db_path: str = field(default_factory=lambda: Path(AppConfig._secret("CHROMA_DB_PATH")).as_posix())
    litellm_url: str = field(default_factory=lambda: AppConfig._secret("LITELLM_URL"))
    litellm_api_key: str = field(default_factory=lambda: AppConfig._secret("LITELLM_API_KEY"))
    confluence_url: str = field(default_factory=lambda: AppConfig._secret("CONFLUENCE_URL"))
    confluence_token: str = field(default_factory=lambda: AppConfig._secret("CONFLUENCE_TOKEN"))
    default_collection: str = field(default_factory=lambda: AppConfig._secret("DEFAULT_COLLECTION"))
    default_model: str = field(default_factory=lambda: AppConfig._secret("LLM_MODEL"))
    embedding_model: str = field(default_factory=lambda: AppConfig._secret("EMBEDDING_MODEL"))
    llm_timeout: int = field(default_factory=lambda: int(AppConfig._secret("LLM_TIMEOUT", "120")))
    embedding_timeout: int = field(default_factory=lambda: int(AppConfig._secret("EMBEDDING_TIMEOUT", "120")))
    log_level: str = field(default_factory=lambda: AppConfig._secret("LOG_LEVEL", "INFO"))

    @staticmethod
    def _secret(key: str, default: str = "") -> str:
        """Получить значение из переменной окружения."""
        return os.environ.get(key, default)

    @property
    def litellm_base_url(self) -> str:
        """Базовый URL без /v1 — для эмбеддингов и прямых запросов."""
        return self.litellm_url.rstrip("/").removesuffix("/v1")

    @property
    def openai_url(self) -> str:
        """URL с /v1 — для OpenAI-совместимого API."""
        return f"{self.litellm_base_url}/v1"
