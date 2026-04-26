"""Конфигурация ChromaDB-extension.

Читается **один раз** при первом вызове ``register_tools``:

* ``persist_path`` — из общей env-переменной ``CHROMA_PERSIST_PATH``,
  той же что использует :mod:`boba_cli_vector_index`. Так оператор
  индексирует CLI-ом и сразу видит коллекции в агенте без дублирования
  путей в конфиге.
* остальные поля (``embedding_model``, ``max_top_k``, ``snippet_chars``)
  — из namespaced bag :attr:`AppConfig.extensions` (секция
  ``extensions.chromadb`` в TOML или env-переменные
  ``BOBA_EXT_CHROMADB__*``).

Семантика поля ``embedding_model`` в v0.1: поддерживается только
``default`` — встроенная ONNX-модель ChromaDB. Поле оставлено в
конфиге как явный контракт на будущее (когда добавим поддержку
``sentence-transformers``-моделей через optional dep), чтобы оператор
не настраивал «бессмысленную» переменную.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from boba.domain.config import AppConfig

NAMESPACE = "chromadb"
PERSIST_PATH_ENV = "CHROMA_PERSIST_PATH"
SUPPORTED_EMBEDDING_MODELS = frozenset({"default"})


class ChromaExtConfigError(Exception):
    """Ошибка чтения/валидации конфига расширения. Сообщение указывает
    конкретный env-ключ, который должен задать оператор.
    """


@dataclass(frozen=True)
class ChromaExtConfig:
    persist_path: str
    embedding_model: str
    max_top_k: int
    snippet_chars: int

    @classmethod
    def from_app_config(cls, app_config: AppConfig) -> ChromaExtConfig:
        persist_path = os.environ.get(PERSIST_PATH_ENV)
        if not persist_path:
            raise ChromaExtConfigError(
                f"chromadb extension: required env var "
                f"{PERSIST_PATH_ENV!r} is missing — set the path to "
                f"chromadb persist directory (the same value as for "
                f"boba-cli-vector-index)"
            )
        section: Mapping[str, str] = app_config.extensions.get(NAMESPACE, {})
        embedding_model = section.get("embedding_model", "default")
        if embedding_model not in SUPPORTED_EMBEDDING_MODELS:
            raise ChromaExtConfigError(
                f"BOBA_EXT_CHROMADB__EMBEDDING_MODEL={embedding_model!r} "
                f"is not supported in v0.1; only "
                f"{sorted(SUPPORTED_EMBEDDING_MODELS)} accepted"
            )
        return cls(
            persist_path=persist_path,
            embedding_model=embedding_model,
            max_top_k=cls._int(section, "max_top_k", 20),
            snippet_chars=cls._int(section, "snippet_chars", 300),
        )

    @staticmethod
    def _int(section: Mapping[str, str], key: str, default: int) -> int:
        raw = section.get(key)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError as e:
            env_key = f"BOBA_EXT_{NAMESPACE.upper()}__{key.upper()}"
            raise ChromaExtConfigError(
                f"chromadb extension config: {key!r}={raw!r} is not "
                f"a valid int (env {env_key})"
            ) from e
