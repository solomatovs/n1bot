"""Env-variable ConfigSource-реализации.

Алгоритм мапинга ConfigKey → env-имя:

    "_".join([ENV_PREFIX, *key.parts]).upper()

Например, ConfigKey("ext","chromadb","persist_path") →
BOBA_EXT_CHROMADB_PERSIST_PATH. Никаких алиасов, никаких legacy-имён.

EnvFileSource дополнительно ищет тот же ключ с суффиксом
_FILE — Docker-style секрет: env указывает путь к файлу,
содержимое читается и обрезается trailing-whitespace.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Final

from boba.domain.core.config import ConfigKey, ConfigSource
from boba.domain.core.declaration import FieldSpec

__all__ = [
    "ENV_FILE_SUFFIX",
    "ENV_PREFIX",
    "EnvFileSource",
    "EnvSource",
    "env_name",
]


logger = logging.getLogger(__name__)


ENV_PREFIX: Final[str] = "BOBA"
"""Префикс всех env-имён, выводимых из ConfigKey."""

ENV_FILE_SUFFIX: Final[str] = "_FILE"
"""Суффикс env-имени для секрет-указателя на файл (Docker-style)."""


def env_name(key: ConfigKey) -> str:
    """ConfigKey → env-имя по единому алгоритму.

    Чистая функция, доступна публично — пригодится для генерации
    operator-доки и сообщений об ошибках («задайте через env-переменную
    {env_name(key)}»).
    """
    return "_".join((ENV_PREFIX, *key.parts)).upper()


_BOBA_PREFIX_WITH_UNDERSCORE: Final[str] = ENV_PREFIX + "_"


def _is_boba_var(name: str) -> bool:
    """Имя начинается с ``BOBA_`` — кандидат на конфиг-переменную."""
    return name.startswith(_BOBA_PREFIX_WITH_UNDERSCORE)


def _is_secret_pointer(name: str) -> bool:
    """Имя оканчивается на ``_FILE`` — Docker-style pointer на секрет."""
    return name.endswith(ENV_FILE_SUFFIX)


def _strip_file_suffix(name: str) -> str:
    """Убрать ``_FILE`` с конца имени; вызывать только для имён, для
    которых :func:`_is_secret_pointer` вернул True."""
    return name[: -len(ENV_FILE_SUFFIX)]


class EnvSource(ConfigSource):
    """Читает значение из os.environ по имени, выведенному из
    ConfigKey через env_name.

    На bind_schema опционально проверяет ``BOBA_*`` переменные на
    опечатки — те, что не соответствуют ни одному
    зарегистрированному ConfigKey. ``BOBA_*_FILE`` исключаются из
    проверки — это территория EnvFileSource.

    Параметр strict:

    * False (default) — неизвестные ``BOBA_*`` логируются как warning
      (logger ``boba.config.env.source``);
    * True — неизвестные ``BOBA_*`` поднимают ValueError, останавливая
      сборку конфига. Полезно в строгих pipeline'ах, где опечатка
      должна валить деплой, а не уезжать в прод тихо.

    Параметр ``extra_known`` — набор имён, которые ожидаются в
    окружении, но не выводятся из схемы (например, bootstrap-мета
    ``BOBA_CONFIG_PATH``, читаемая ``TomlSource`` до сборки бандла).
    Bootstrap-код передаёт сюда константы соответствующих пакетов.
    """

    def __init__(
        self,
        *,
        strict: bool = False,
        extra_known: Iterable[str] = (),
    ) -> None:
        self._strict = strict
        self._extra_known = frozenset(extra_known)

    def bind_schema(
        self,
        items: Iterable[tuple[ConfigKey, FieldSpec[Any]]],
    ) -> None:
        known_names = {env_name(key) for key, _field in items} | self._extra_known
        unknown: list[str] = []
        for actual in os.environ:
            if not _is_boba_var(actual):
                continue
            if _is_secret_pointer(actual):
                continue  # *_FILE — забота EnvFileSource
            if actual in known_names:
                continue
            unknown.append(actual)
        if not unknown:
            return
        msg = (
            f"unknown {ENV_PREFIX}_* env vars (typo? schema not registered?): "
            f"{sorted(unknown)}"
        )
        if self._strict:
            raise ValueError(msg)
        logger.warning(msg)

    def resolve(self, key: ConfigKey) -> object | None:
        return os.environ.get(env_name(key))

    def describe(self, key: ConfigKey) -> str:
        return f"env {env_name(key)}"


class EnvFileSource(ConfigSource):
    """Читает значение из файла, путь к которому хранит env-переменная
    {env_name(key)}_FILE.

    Если переменная не задана или файл не существует — None
    (последующие источники продолжают). Содержимое возвращается с
    обрезанным trailing-whitespace.

    bind_schema симметричен EnvSource'у: проверяет ``BOBA_*_FILE``
    переменные на соответствие схеме (после strip'а суффикса
    ``_FILE``). См. описание strict / extra_known у :class:`EnvSource`.
    """

    def __init__(
        self,
        *,
        strict: bool = False,
        extra_known: Iterable[str] = (),
    ) -> None:
        self._strict = strict
        self._extra_known = frozenset(extra_known)

    def bind_schema(
        self,
        items: Iterable[tuple[ConfigKey, FieldSpec[Any]]],
    ) -> None:
        known_names = {env_name(key) for key, _field in items} | self._extra_known
        unknown: list[str] = []
        for actual in os.environ:
            if not _is_boba_var(actual):
                continue
            if not _is_secret_pointer(actual):
                continue  # обычные BOBA_* — забота EnvSource
            if _strip_file_suffix(actual) in known_names:
                continue
            unknown.append(actual)
        if not unknown:
            return
        msg = (
            f"unknown {ENV_PREFIX}_*{ENV_FILE_SUFFIX} env vars "
            f"(typo? schema not registered?): {sorted(unknown)}"
        )
        if self._strict:
            raise ValueError(msg)
        logger.warning(msg)

    def resolve(self, key: ConfigKey) -> object | None:
        path = os.environ.get(env_name(key) + ENV_FILE_SUFFIX)
        if not path:
            return None
        secret_file = Path(path)
        if not secret_file.is_file():
            return None
        return secret_file.read_text(encoding="utf-8").strip()

    def describe(self, key: ConfigKey) -> str:
        return f"env {env_name(key)}{ENV_FILE_SUFFIX}=<path-to-secret-file>"
