"""Конфигурация приложения.

Источники (приоритет от высшего к низшему):
    1. Env var <KEY>_FILE — путь к файлу с секретом
    2. Env var <KEY> — переменная окружения
    3. TOML-файл (секция [app]) — путь задаётся через BOBA_CONFIG
    4. Значение по умолчанию
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from boba_domain import toml_config

# Lazy-загрузка секции [app] — один раз при создании первого AppConfig
_app_toml: dict[str, Any] | None = None


def _get_app_toml() -> dict[str, Any]:
    global _app_toml
    if _app_toml is None:
        _app_toml = toml_config.load_section("app")
    return _app_toml


@dataclass(frozen=True)
class AppConfig:
    """Единственный источник конфигурации приложения."""

    _litellm_url: str = field(
        default_factory=lambda: toml_config.resolve(
            "LITELLM_URL", _get_app_toml(), "litellm_url"
        )
    )
    _litellm_api_key: str = field(
        default_factory=lambda: toml_config.resolve(
            "LITELLM_API_KEY", _get_app_toml(), "litellm_api_key"
        )
    )
    _confluence_url: str = field(
        default_factory=lambda: toml_config.resolve(
            "CONFLUENCE_URL", _get_app_toml(), "confluence_url"
        )
    )
    _confluence_token: str = field(
        default_factory=lambda: toml_config.resolve(
            "CONFLUENCE_TOKEN", _get_app_toml(), "confluence_token"
        )
    )
    _default_collection: str = field(
        default_factory=lambda: toml_config.resolve(
            "DEFAULT_COLLECTION", _get_app_toml(), "default_collection"
        )
    )
    _embedding_model: str = field(
        default_factory=lambda: toml_config.resolve(
            "EMBEDDING_MODEL", _get_app_toml(), "embedding_model"
        )
    )
    _llm_timeout: int = field(
        default_factory=lambda: int(
            toml_config.resolve("LLM_TIMEOUT", _get_app_toml(), "llm_timeout", "120")
        )
    )
    _embedding_timeout: int = field(
        default_factory=lambda: int(
            toml_config.resolve(
                "EMBEDDING_TIMEOUT", _get_app_toml(), "embedding_timeout", "120"
            )
        )
    )
    _ssl_verify: bool = field(
        default_factory=lambda: toml_config.resolve(
            "SSL_VERIFY", _get_app_toml(), "ssl_verify", "false"
        ).lower()
        in ("true", "1", "yes")
    )
    _import_base_dir: str = field(
        default_factory=lambda: toml_config.resolve(
            "IMPORT_BASE_DIR", _get_app_toml(), "import_base_dir", "./import"
        )
    )
    _boba_dir_name: str = field(
        default_factory=lambda: toml_config.resolve(
            "BOBA_DIR_NAME", _get_app_toml(), "boba_dir_name", ".boba"
        )
    )
    _context_dir_name: str = field(
        default_factory=lambda: toml_config.resolve(
            "CONTEXT_DIR_NAME", _get_app_toml(), "context_dir_name", "context"
        )
    )
    _chroma_dir_name: str = field(
        default_factory=lambda: toml_config.resolve(
            "CHROMA_DIR_NAME", _get_app_toml(), "chroma_dir_name", "chroma"
        )
    )
    _chat_history_filename: str = field(
        default_factory=lambda: toml_config.resolve(
            "CHAT_HISTORY_FILENAME",
            _get_app_toml(),
            "chat_history_filename",
            "chat_history.jsonl",
        )
    )
    _index_manifest_filename: str = field(
        default_factory=lambda: toml_config.resolve(
            "INDEX_MANIFEST_FILENAME",
            _get_app_toml(),
            "index_manifest_filename",
            "index_manifest.json",
        )
    )
    _collection_prefix: str = field(
        default_factory=lambda: toml_config.resolve(
            "COLLECTION_PREFIX", _get_app_toml(), "collection_prefix", "doc"
        )
    )
    _log_level: str = field(
        default_factory=lambda: toml_config.resolve(
            "LOG_LEVEL", _get_app_toml(), "log_level", "INFO"
        )
    )

    @property
    def litellm_api_key(self) -> str:
        return self._litellm_api_key

    @property
    def confluence_url(self) -> str:
        return self._confluence_url

    @property
    def confluence_content_url(self) -> str:
        return f"{self._confluence_url}/rest/api/content"

    def confluence_page_url(self, page_id: str) -> str:
        return f"{self._confluence_url}/rest/api/content/{page_id}"

    @property
    def confluence_token(self) -> str:
        return self._confluence_token

    @property
    def confluence_auth_headers(self) -> dict[str, str]:
        return self.confluence_bearer_headers(self._confluence_token)

    @staticmethod
    def confluence_bearer_headers(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    @property
    def default_collection(self) -> str:
        return self._default_collection

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
        return self._litellm_url.rstrip("/").removesuffix("/v1")

    @property
    def openai_url(self) -> str:
        return f"{self.litellm_base_url}/v1"

    @property
    def litellm_models_url(self) -> str:
        return f"{self.litellm_base_url}/v1/models"

    @property
    def litellm_auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._litellm_api_key}"}

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
        return folder / self._boba_dir_name

    def chroma_path(self, folder: Path) -> Path:
        return self.boba_path(folder) / self._chroma_dir_name

    def thread_path(self, folder: Path) -> Path:
        """Путь к единственному thread.json workspace'а."""
        return self.boba_path(folder) / "thread.json"

    def workspace_history_path(self, folder: Path) -> Path:
        """Путь к единственному chat_history.jsonl workspace'а."""
        return self.boba_path(folder) / self._chat_history_filename

    def workspace_path(self, folder_name: str) -> Path:
        """Полный путь к папке workspace'а."""
        return Path(self._import_base_dir) / folder_name

    def index_manifest_path(self, folder: Path) -> Path:
        return self.boba_path(folder) / self._index_manifest_filename

    def collection_name(self, folder_name: str) -> str:
        return _sanitize_collection_name(folder_name, self._collection_prefix)


def _sanitize_collection_name(folder_name: str, prefix: str) -> str:
    import re

    name = re.sub(r"[^a-zA-Z0-9._-]", "_", folder_name)
    name = f"{prefix}.{name}"
    name = name.strip("._-") or f"{prefix}.default"
    if len(name) < 3:
        name = name.ljust(3, "0")
    return name[:512]
