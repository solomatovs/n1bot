"""Имена объектов стенда на общих серверах: базы и схемы postgres, базы
ClickHouse, пользователи Oracle.

Серверы стенда общие у всех разработчиков машины, а наборы тестов разных
пакетов и процессы pytest-xdist внутри набора идут параллельно против них.
Поэтому имя несёт метку разработчика ([site].developer конфига стенда), набора
и процесса, и никто не пересоздаёт и не сносит чужие данные. Метки ставит
StandSuite в pytest_configure — раньше импорта модулей тестов, где имена
подставляются в SQL: корневой conftest выводит метку набора из пути пакета,
conftest набора может задать свою короткую.

Базы и схемы, которые прогон не пересоздаёт, а доводит до раскладки своего
кода (базы приложения, схемы истории агента), получают ещё и метку дерева —
каталога, из которого запущен pytest: worktree с разными миграциями не
ломают такие объекты друг другу (of_tree). Базы postgres начинаются с aid_,
как база разработчика aid_<developer>: aid_ss_acp_chat_aid_gw3_pint4.

Ошибки:
StandError — конфиг стенда не читается, или имя длиннее 63 байт: postgres
    молча обрезал бы его, и имена разных наборов совпали бы.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

import pytest

from boba.stand.site import Stand, StandError

__all__ = ["NamePart", "StandNames", "StandSuite"]


class NamePart(StrEnum):
    """Постоянные части имён стенда."""

    SEPARATOR = "_"
    DATABASE = "aid"


class XdistKey(StrEnum):
    """Ключи xdist в config.workerinput."""

    WORKER = "workerid"


class StandNames:
    """Имя объекта стенда с меткой разработчика, набора и процесса xdist:
    ss_ix_stand_cfl_gw3; база postgres — с приставкой aid_.

    Метки живут на процесс: их ставит StandSuite, читают стенды пакетов,
    когда собирают имена своих объектов."""

    MAX_BYTES: ClassVar[int] = 63
    """Предел имени postgres (NAMEDATALEN - 1)."""

    _developer: ClassVar[str] = ""
    _suite: ClassVar[str] = ""
    _worker: ClassVar[str] = ""
    _tree: ClassVar[str] = ""

    def use_developer(self, developer: str) -> None:
        StandNames._developer = developer

    def use_suite(self, suite: str) -> None:
        StandNames._suite = suite

    def use_worker(self, worker: str) -> None:
        StandNames._worker = worker

    def use_tree(self, tree: str) -> None:
        StandNames._tree = tree

    def of(self, name: str) -> str:
        """Имя с меткой разработчика, набора и процесса: ss_describe_stand_ch_gw1."""
        return self._checked(self._labeled(name))

    def of_tree(self, name: str) -> str:
        """Имя с меткой разработчика, набора, процесса и дерева:
        ss_agent_service_mode_aid_gw3_ptspeed."""
        return self._checked(self._tree_labeled(name))

    def database(self, name: str) -> str:
        """База postgres без метки дерева: aid_ss_ix_stand_cfl_gw3."""
        return self._checked(
            NamePart.SEPARATOR.join((NamePart.DATABASE, self._labeled(name)))
        )

    def tree_database(self, name: str) -> str:
        """База postgres с меткой дерева: aid_ss_acp_chat_aid_gw3_pint4."""
        labeled = self._tree_labeled(name)
        return self._checked(NamePart.SEPARATOR.join((NamePart.DATABASE, labeled)))

    def _labeled(self, name: str) -> str:
        if not StandNames._developer:
            msg = (
                f"stand names: naming {name!r} needs the developer label; "
                "StandSuite.configure sets it from [site].developer of the stand config"
            )
            raise StandError(msg)

        parts = [StandNames._developer, name]
        if StandNames._suite:
            parts.append(StandNames._suite)

        if StandNames._worker:
            parts.append(StandNames._worker)

        return NamePart.SEPARATOR.join(parts)

    def _tree_labeled(self, name: str) -> str:
        labeled = self._labeled(name)
        if not StandNames._tree:
            return labeled

        return NamePart.SEPARATOR.join((labeled, StandNames._tree))

    def _checked(self, name: str) -> str:
        size = len(name.encode())
        if size > self.MAX_BYTES:
            msg = (
                f"stand names: {name!r} takes {size} bytes, postgres keeps "
                f"{self.MAX_BYTES} and would cut it; shorten the base name, the "
                "suite label or the worktree directory"
            )
            raise StandError(msg)

        return name


class StandSuite:
    """Метки процесса pytest в именах стенда: разработчика берёт из конфига
    стенда (Stand), набор объявляет conftest пакета, номер процесса приходит
    от xdist, без xdist его нет."""

    def __init__(self, config: pytest.Config) -> None:
        self._config = config
        self._names = StandNames()

    TESTS_DIR: ClassVar[str] = "tests"
    TREE_DROPPED: ClassVar[re.Pattern[str]] = re.compile(r"[^a-z0-9_]")
    """Что в имени каталога дерева не годится для имени объекта postgres."""

    def configure(self, suite: str) -> None:
        self._names.use_developer(Stand.load().developer)
        self._names.use_suite(suite)
        tree = self._config.rootpath.name.lower()
        self._names.use_tree(self.TREE_DROPPED.sub(NamePart.SEPARATOR, tree))

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
            suite = packages.pop().replace("-", NamePart.SEPARATOR)

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
