"""Имена объектов стенда на общих серверах: базы и схемы postgres, базы
ClickHouse, пользователи Oracle.

Наборы тестов разных пакетов и процессы pytest-xdist внутри набора идут
параллельно против одних серверов. Каждый набор и процесс получает свою метку
в именах, поэтому никто не пересоздаёт и не сносит чужие данные. Метку ставит
StandSuite в pytest_configure — раньше импорта модулей тестов, где имена
подставляются в SQL: корневой conftest выводит её из пути пакета, conftest
набора может задать свою короткую.

Ошибок наружу нет.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

import pytest

__all__ = ["StandNames", "StandSuite"]


class XdistKey(StrEnum):
    """Ключи xdist в config.workerinput."""

    WORKER = "workerid"


class StandNames:
    """Имя объекта стенда с меткой набора и процесса xdist: ix_stand_cfl_gw3.

    Метка живёт на процесс: её ставит StandSuite, читают стенды пакетов,
    когда собирают имена своих объектов."""

    SEPARATOR: ClassVar[str] = "_"

    _suite: ClassVar[str] = ""
    _worker: ClassVar[str] = ""

    def use_suite(self, suite: str) -> None:
        StandNames._suite = suite

    def use_worker(self, worker: str) -> None:
        StandNames._worker = worker

    def of(self, name: str) -> str:
        parts = [name]
        if StandNames._suite:
            parts.append(StandNames._suite)

        if StandNames._worker:
            parts.append(StandNames._worker)

        return self.SEPARATOR.join(parts)


class StandSuite:
    """Метка процесса pytest в именах стенда: набор объявляет conftest пакета,
    номер процесса приходит от xdist, без xdist его нет."""

    def __init__(self, config: pytest.Config) -> None:
        self._config = config
        self._names = StandNames()

    TESTS_DIR: ClassVar[str] = "tests"

    def configure(self, suite: str) -> None:
        self._names.use_suite(suite)

        workerinput: dict[str, Any] | None = getattr(self._config, "workerinput", None)
        if workerinput is None:
            return

        self._names.use_worker(str(workerinput[XdistKey.WORKER]))

    def configure_package(self, args: Sequence[str]) -> None:
        """Метка по пакету, чьи тесты запущены: boba-tool-knowledge →
        boba_tool_knowledge. Пути из разных пакетов метки не дают."""
        packages = set(self._packages(args))
        suite = ""
        if len(packages) == 1:
            suite = packages.pop().replace("-", self._names.SEPARATOR)

        self.configure(suite)

    def _packages(self, args: Sequence[str]) -> list[str]:
        found: list[str] = []
        for arg in args:
            path = Path(arg.split("::", 1)[0]).resolve()
            parts = path.parts
            if self.TESTS_DIR not in parts:
                continue

            index = parts.index(self.TESTS_DIR)
            if index == 0:
                continue

            found.append(parts[index - 1])

        return found
