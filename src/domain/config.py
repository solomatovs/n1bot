"""Конфигурация приложения."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class AppConfig:
    """Единственный источник конфигурации приложения.

    Все секреты и переменные окружения читаются здесь.
    Остальные модули получают значения через свойства.
    """
    _chroma_db_path: str = field(default_factory=lambda: Path(AppConfig._secret("CHROMA_DB_PATH")).as_posix())
    _litellm_url: str = field(default_factory=lambda: AppConfig._secret("LITELLM_URL"))
    _litellm_api_key: str = field(default_factory=lambda: AppConfig._secret("LITELLM_API_KEY"))
    _confluence_url: str = field(default_factory=lambda: AppConfig._secret("CONFLUENCE_URL"))
    _confluence_token: str = field(default_factory=lambda: AppConfig._secret("CONFLUENCE_TOKEN"))
    _default_collection: str = field(default_factory=lambda: AppConfig._secret("DEFAULT_COLLECTION"))
    _default_model: str = field(default_factory=lambda: AppConfig._secret("LLM_MODEL"))
    _embedding_model: str = field(default_factory=lambda: AppConfig._secret("EMBEDDING_MODEL"))
    _llm_timeout: int = field(default_factory=lambda: int(AppConfig._secret("LLM_TIMEOUT", "120")))
    _embedding_timeout: int = field(default_factory=lambda: int(AppConfig._secret("EMBEDDING_TIMEOUT", "120")))
    _ssl_verify: bool = field(default_factory=lambda: AppConfig._secret("SSL_VERIFY", "false").lower() in ("true", "1", "yes"))
    _log_level: str = field(default_factory=lambda: AppConfig._secret("LOG_LEVEL", "INFO"))

    @staticmethod
    def _secret(key: str, default: str = "") -> str:
        """Получить значение из переменной окружения."""
        return os.environ.get(key, default)

    @property
    def chroma_db_path(self) -> str:
        return self._chroma_db_path

    @property
    def litellm_api_key(self) -> str:
        return self._litellm_api_key

    @property
    def confluence_url(self) -> str:
        return self._confluence_url

    @property
    def confluence_content_url(self) -> str:
        """URL REST API для работы с контентом Confluence."""
        return f"{self._confluence_url}/rest/api/content"

    @property
    def confluence_token(self) -> str:
        return self._confluence_token

    @property
    def confluence_auth_headers(self) -> dict[str, str]:
        """HTTP-заголовки авторизации для Confluence REST API."""
        return {"Authorization": f"Bearer {self._confluence_token}"}

    @property
    def default_collection(self) -> str:
        return self._default_collection

    @property
    def default_model(self) -> str:
        return self._default_model

    @property
    def embedding_model(self) -> str:
        return self._embedding_model

    @property
    def llm_timeout(self) -> int:
        return self._llm_timeout

    @property
    def embedding_timeout(self) -> int:
        return self._embedding_timeout

    @property
    def ssl_verify(self) -> bool:
        return self._ssl_verify

    @property
    def log_level(self) -> str:
        return self._log_level

    @property
    def litellm_base_url(self) -> str:
        """Базовый URL без /v1 — для эмбеддингов и прямых запросов."""
        return self._litellm_url.rstrip("/").removesuffix("/v1")

    @property
    def openai_url(self) -> str:
        """URL с /v1 — для OpenAI-совместимого API."""
        return f"{self.litellm_base_url}/v1"

    @property
    def litellm_models_url(self) -> str:
        """URL для получения списка моделей (/v1/models)."""
        return f"{self.litellm_base_url}/v1/models"

    @property
    def litellm_auth_headers(self) -> dict[str, str]:
        """HTTP-заголовки авторизации для LiteLLM API."""
        return {"Authorization": f"Bearer {self._litellm_api_key}"}
