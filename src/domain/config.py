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
    _import_base_dir: str = field(default_factory=lambda: AppConfig._secret("IMPORT_BASE_DIR", "./import"))
    _boba_dir_name: str = field(default_factory=lambda: AppConfig._secret("BOBA_DIR_NAME", ".boba"))
    _context_dir_name: str = field(default_factory=lambda: AppConfig._secret("CONTEXT_DIR_NAME", "context"))
    _chroma_dir_name: str = field(default_factory=lambda: AppConfig._secret("CHROMA_DIR_NAME", "chroma"))
    _chat_history_filename: str = field(default_factory=lambda: AppConfig._secret("CHAT_HISTORY_FILENAME", "chat_history.md"))
    _index_manifest_filename: str = field(default_factory=lambda: AppConfig._secret("INDEX_MANIFEST_FILENAME", "index_manifest.json"))
    _collection_prefix: str = field(default_factory=lambda: AppConfig._secret("COLLECTION_PREFIX", "doc"))
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

    def confluence_page_url(self, page_id: str) -> str:
        """URL REST API для конкретной страницы Confluence."""
        return f"{self._confluence_url}/rest/api/content/{page_id}"

    @property
    def confluence_token(self) -> str:
        return self._confluence_token

    @property
    def confluence_auth_headers(self) -> dict[str, str]:
        """HTTP-заголовки авторизации для Confluence REST API."""
        return self.confluence_bearer_headers(self._confluence_token)

    @staticmethod
    def confluence_bearer_headers(token: str) -> dict[str, str]:
        """Сформировать Bearer-заголовки для Confluence."""
        return {"Authorization": f"Bearer {token}"}

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
    def import_base_dir(self) -> str:
        return self._import_base_dir

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

    # ---------------------------------------------------------------------------
    # Пути .boba — системная директория агента внутри папки с документами
    # ---------------------------------------------------------------------------

    @property
    def boba_dir_name(self) -> str:
        return self._boba_dir_name

    @property
    def context_dir_name(self) -> str:
        return self._context_dir_name

    @property
    def chroma_dir_name(self) -> str:
        return self._chroma_dir_name

    @property
    def chat_history_filename(self) -> str:
        return self._chat_history_filename

    @property
    def collection_prefix(self) -> str:
        return self._collection_prefix

    def boba_path(self, folder: Path) -> Path:
        """Путь к .boba внутри папки с документами."""
        return folder / self._boba_dir_name

    def context_path(self, folder: Path) -> Path:
        """Путь к директории конвертированных markdown-документов."""
        return self.boba_path(folder) / self._context_dir_name

    def context_file_path(self, folder: Path, filename: str) -> Path:
        """Путь к конкретному файлу в директории контекста."""
        return self.context_path(folder) / filename

    def chroma_path(self, folder: Path) -> Path:
        """Путь к локальному ChromaDB хранилищу."""
        return self.boba_path(folder) / self._chroma_dir_name

    def chat_history_path(self, folder: Path) -> Path:
        """Путь к файлу истории чата."""
        return self.boba_path(folder) / self._chat_history_filename

    def index_manifest_path(self, folder: Path) -> Path:
        """Путь к манифесту индексации (хеши файлов)."""
        return self.boba_path(folder) / self._index_manifest_filename

    def collection_name(self, folder_name: str) -> str:
        """Имя коллекции ChromaDB для папки с документами."""
        return _sanitize_collection_name(folder_name, self._collection_prefix)


def _sanitize_collection_name(folder_name: str, prefix: str) -> str:
    """Привести имя папки к допустимому имени коллекции ChromaDB.

    Допустимы: [a-zA-Z0-9._-], 3-512 символов,
    начинается и заканчивается на [a-zA-Z0-9].
    """
    import re
    name = re.sub(r"[^a-zA-Z0-9._-]", "_", folder_name)
    name = f"{prefix}.{name}"
    name = name.strip("._-") or f"{prefix}.default"
    if len(name) < 3:
        name = name.ljust(3, "0")
    return name[:512]
