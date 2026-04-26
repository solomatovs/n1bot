"""Конфигурация ChromaDB-extension.

Читается **один раз** при первом вызове ``register_tools`` из namespaced
bag :attr:`AppConfig.extensions` (см. секцию ``extensions.chromadb`` в
TOML или env-переменные ``BOBA_EXT_CHROMADB__*``). Дальше передаётся
во все Tool-инстансы через конструктор и в singleton-фабрику KB.

Семантика поля ``embedding_model`` в v0.1: поддерживается только
``default`` — встроенная ONNX-модель ChromaDB. Поле оставлено в
конфиге как явный контракт на будущее (когда добавим поддержку
``sentence-transformers``-моделей через optional dep), чтобы оператор
не настраивал «бессмысленную» переменную.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from boba.domain.config import AppConfig

NAMESPACE = "chromadb"
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
        section: Mapping[str, str] = app_config.extensions.get(NAMESPACE, {})
        persist_path = cls._required(section, "persist_path")
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
    def _required(section: Mapping[str, str], key: str) -> str:
        value = section.get(key)
        if not value:
            env_key = f"BOBA_EXT_{NAMESPACE.upper()}__{key.upper()}"
            raise ChromaExtConfigError(
                f"chromadb extension config: required field {key!r} "
                f"is missing — set {env_key} or "
                f"[extensions.{NAMESPACE}] {key} in TOML"
            )
        return value

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
