"""env-источник конфига. Имя — env_name(key) = ``BOBA_<PARTS>``."""

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
ENV_FILE_SUFFIX: Final[str] = "_FILE"


def env_name(key: ConfigKey) -> str:
    """ConfigKey → ``BOBA_A_B_C``."""
    return "_".join((ENV_PREFIX, *key.parts)).upper()


class _EnvSourceBase(ConfigSource):
    """Общий каркас env-источников.

    Знает префикс ``BOBA_`` и суффикс ``_FILE``, хранит strict +
    extra_known, рапортует о найденных typo'ах. Наследники — EnvSource
    (``BOBA_X``) и EnvFileSource (``BOBA_X_FILE``) — реализуют только
    bind_schema/resolve/describe.
    """

    def __init__(
        self,
        *,
        strict: bool = False,
        extra_known: Iterable[str] = (),
    ) -> None:
        """Запомнить strict + extra_known."""
        self._strict = strict
        self._extra_known = frozenset(extra_known)

    @staticmethod
    def _is_boba_var(name: str) -> bool:
        """Имя начинается с ``BOBA_`` — наша переменная."""
        return name.startswith(ENV_PREFIX + "_")

    @staticmethod
    def _is_secret_pointer(name: str) -> bool:
        """Имя оканчивается на ``_FILE`` — Docker-style указатель на секрет."""
        return name.endswith(ENV_FILE_SUFFIX)

    @staticmethod
    def _strip_file_suffix(name: str) -> str:
        """Убрать ``_FILE``; вызывать только после :meth:`_is_secret_pointer`."""
        return name[: -len(ENV_FILE_SUFFIX)]

    def _report_unknown(self, unknown: list[str], suffix: str = "") -> None:
        """warning/ValueError (по strict) на найденные имена; suffix — после ``*``."""
        if not unknown:
            return
        msg = f"unknown {ENV_PREFIX}_*{suffix} env vars: {sorted(unknown)}"
        if self._strict:
            raise ValueError(msg)
        logger.warning(msg)


class EnvSource(_EnvSourceBase):
    """Значение из ``os.environ[env_name(key)]``.

    bind_schema проверяет ``BOBA_*`` env vars на соответствие схеме
    (``BOBA_*_FILE`` пропускаются — это EnvFileSource). См.
    :class:`_EnvSourceBase` про strict + extra_known.
    """

    def bind_schema(
        self,
        items: Iterable[tuple[ConfigKey, FieldSpec[Any]]],
    ) -> None:
        """Сравнить ``BOBA_*`` (без ``_FILE``) с known_names; ругнуться на лишние."""
        known_names = {env_name(key) for key, _field in items} | self._extra_known
        unknown: list[str] = []
        for actual in os.environ:
            if not self._is_boba_var(actual):
                continue
            if self._is_secret_pointer(actual):
                continue
            if actual in known_names:
                continue
            unknown.append(actual)
        self._report_unknown(unknown)

    def resolve(self, key: ConfigKey) -> object | None:
        """Значение ``os.environ[BOBA_*]`` или None."""
        return os.environ.get(env_name(key))

    def describe(self, key: ConfigKey) -> str:
        """Operator-readable hint: ``env BOBA_A_B``."""
        return f"env {env_name(key)}"


class EnvFileSource(_EnvSourceBase):
    """Значение из файла ``${env_name(key)}_FILE`` (Docker-style).

    bind_schema проверяет ``BOBA_*_FILE`` env vars на соответствие схеме
    (после strip ``_FILE``). См. :class:`_EnvSourceBase` про
    strict + extra_known.
    """

    def bind_schema(
        self,
        items: Iterable[tuple[ConfigKey, FieldSpec[Any]]],
    ) -> None:
        """Сравнить ``BOBA_*_FILE`` со схемой (strip ``_FILE``); ругнуться на лишние."""
        known_names = {env_name(key) for key, _field in items} | self._extra_known
        unknown: list[str] = []
        for actual in os.environ:
            if not self._is_boba_var(actual):
                continue
            if not self._is_secret_pointer(actual):
                continue
            if self._strip_file_suffix(actual) in known_names:
                continue
            unknown.append(actual)
        self._report_unknown(unknown, ENV_FILE_SUFFIX)

    def resolve(self, key: ConfigKey) -> object | None:
        """Прочитать файл, путь к нему — в ``os.environ[BOBA_*_FILE]``."""
        path = os.environ.get(env_name(key) + ENV_FILE_SUFFIX)
        if not path:
            return None
        secret_file = Path(path)
        if not secret_file.is_file():
            return None
        return secret_file.read_text(encoding="utf-8").strip()

    def describe(self, key: ConfigKey) -> str:
        """Operator-readable hint: ``env BOBA_A_B_FILE=<path>``."""
        return f"env {env_name(key)}{ENV_FILE_SUFFIX}=<path-to-secret-file>"
