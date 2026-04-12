"""Конфигурация приложения.

Источники (приоритет от высшего к низшему):
    1. Env var <KEY>_FILE — путь к файлу с секретом
    2. Env var <KEY> — переменная окружения
    3. TOML-файл (секция [app]) — путь задаётся через BOBA_CONFIG
    4. Значение по умолчанию
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _load_toml_section(section: str) -> dict[str, Any]:
    """Загрузить секцию из TOML-файла конфигурации.

    Путь к файлу задаётся через BOBA_CONFIG.
    Если файл не найден — возвращает пустой dict (все значения из defaults).
    """
    config_path = os.environ.get("BOBA_CONFIG", "")
    if not config_path:
        return {}
    path = Path(config_path)
    if not path.is_file():
        return {}
    try:
        import tomli
        with open(path, "rb") as f:
            data = tomli.load(f)
        return data.get(section, {})
    except Exception:
        return {}


def _resolve(key: str, toml_data: dict[str, Any], toml_key: str, default: str = "") -> str:
    """Получить значение конфигурации.

    Приоритет:
    1. <KEY>_FILE — путь к файлу с секретом
    2. <KEY> — env var
    3. TOML [app].<toml_key>
    4. default
    """
    file_path = os.environ.get(f"{key}_FILE")
    if file_path:
        p = Path(file_path)
        if p.is_file():
            return p.read_text(encoding="utf-8").strip()
    env_val = os.environ.get(key)
    if env_val is not None:
        return env_val
    toml_val = toml_data.get(toml_key)
    if toml_val is not None:
        return str(toml_val)
    return default


# Lazy-загрузка секции [app] — один раз при создании первого AppConfig
_app_toml: dict[str, Any] | None = None


def _get_app_toml() -> dict[str, Any]:
    global _app_toml
    if _app_toml is None:
        _app_toml = _load_toml_section("app")
    return _app_toml


@dataclass(frozen=True)
class AppConfig:
    """Единственный источник конфигурации приложения."""

    _litellm_url: str = field(default_factory=lambda: _resolve("LITELLM_URL", _get_app_toml(), "litellm_url"))
    _litellm_api_key: str = field(default_factory=lambda: _resolve("LITELLM_API_KEY", _get_app_toml(), "litellm_api_key"))
    _confluence_url: str = field(default_factory=lambda: _resolve("CONFLUENCE_URL", _get_app_toml(), "confluence_url"))
    _confluence_token: str = field(default_factory=lambda: _resolve("CONFLUENCE_TOKEN", _get_app_toml(), "confluence_token"))
    _default_collection: str = field(default_factory=lambda: _resolve("DEFAULT_COLLECTION", _get_app_toml(), "default_collection"))
    _embedding_model: str = field(default_factory=lambda: _resolve("EMBEDDING_MODEL", _get_app_toml(), "embedding_model"))
    _llm_timeout: int = field(default_factory=lambda: int(_resolve("LLM_TIMEOUT", _get_app_toml(), "llm_timeout", "120")))
    _embedding_timeout: int = field(default_factory=lambda: int(_resolve("EMBEDDING_TIMEOUT", _get_app_toml(), "embedding_timeout", "120")))
    _ssl_verify: bool = field(default_factory=lambda: _resolve("SSL_VERIFY", _get_app_toml(), "ssl_verify", "false").lower() in ("true", "1", "yes"))
    _import_base_dir: str = field(default_factory=lambda: _resolve("IMPORT_BASE_DIR", _get_app_toml(), "import_base_dir", "./import"))
    _boba_dir_name: str = field(default_factory=lambda: _resolve("BOBA_DIR_NAME", _get_app_toml(), "boba_dir_name", ".boba"))
    _context_dir_name: str = field(default_factory=lambda: _resolve("CONTEXT_DIR_NAME", _get_app_toml(), "context_dir_name", "context"))
    _chroma_dir_name: str = field(default_factory=lambda: _resolve("CHROMA_DIR_NAME", _get_app_toml(), "chroma_dir_name", "chroma"))
    _chats_dir_name: str = field(default_factory=lambda: _resolve("CHATS_DIR_NAME", _get_app_toml(), "chats_dir_name", "chats"))
    _chat_history_filename: str = field(default_factory=lambda: _resolve("CHAT_HISTORY_FILENAME", _get_app_toml(), "chat_history_filename", "chat_history.jsonl"))
    _index_manifest_filename: str = field(default_factory=lambda: _resolve("INDEX_MANIFEST_FILENAME", _get_app_toml(), "index_manifest_filename", "index_manifest.json"))
    _collection_prefix: str = field(default_factory=lambda: _resolve("COLLECTION_PREFIX", _get_app_toml(), "collection_prefix", "doc"))
    _log_level: str = field(default_factory=lambda: _resolve("LOG_LEVEL", _get_app_toml(), "log_level", "INFO"))

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

    def chats_dir(self, folder: Path) -> Path:
        return self.boba_path(folder) / self._chats_dir_name

    def chat_history_path(self, folder: Path, chat_id: str) -> Path:
        return self.chats_dir(folder) / f"{chat_id}.jsonl"

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
