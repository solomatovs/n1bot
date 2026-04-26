"""Env-variable :class:`ConfigSource`-реализации.

Алгоритм мапинга :class:`ConfigKey` → env-имя:

    "_".join([ENV_PREFIX, *key.parts]).upper()

Например, ``ConfigKey("ext","chromadb","persist_path")`` →
``BOBA_EXT_CHROMADB_PERSIST_PATH``. Никаких алиасов, никаких legacy-имён.

:class:`EnvFileSource` дополнительно ищет тот же ключ с суффиксом
``_FILE`` — Docker-style секрет: env указывает путь к файлу,
содержимое читается и обрезается trailing-whitespace.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Final

from boba.domain.core.config import ConfigKey, ConfigSource, FieldSpec

__all__ = [
    "ENV_FILE_SUFFIX",
    "ENV_PREFIX",
    "EnvFileSource",
    "EnvSource",
    "env_name",
]


ENV_PREFIX: Final[str] = "BOBA"
"""Префикс всех env-имён, выводимых из :class:`ConfigKey`."""

ENV_FILE_SUFFIX: Final[str] = "_FILE"
"""Суффикс env-имени для секрет-указателя на файл (Docker-style)."""


def env_name(key: ConfigKey) -> str:
    """``ConfigKey`` → env-имя по единому алгоритму.

    Чистая функция, доступна публично — пригодится для генерации
    operator-доки и сообщений об ошибках («задайте через env-переменную
    {env_name(key)}»).
    """
    return "_".join((ENV_PREFIX, *key.parts)).upper()


class EnvSource(ConfigSource):
    """Читает значение из ``os.environ`` по имени, выведенному из
    :class:`ConfigKey` через :func:`env_name`.
    """

    def resolve(self, spec: FieldSpec[Any]) -> object | None:
        return os.environ.get(env_name(spec.key))


class EnvFileSource(ConfigSource):
    """Читает значение из файла, путь к которому хранит env-переменная
    ``{env_name(key)}_FILE``.

    Если переменная не задана или файл не существует — ``None``
    (последующие источники продолжают). Содержимое возвращается с
    обрезанным trailing-whitespace.
    """

    def resolve(self, spec: FieldSpec[Any]) -> object | None:
        path = os.environ.get(env_name(spec.key) + ENV_FILE_SUFFIX)
        if not path:
            return None
        p = Path(path)
        if not p.is_file():
            return None
        return p.read_text(encoding="utf-8").strip()
