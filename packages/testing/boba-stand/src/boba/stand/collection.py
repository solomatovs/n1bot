"""Собранные тесты прогона глазами наборов: группы pytest-xdist и обязательное
окружение теста.

Хук pytest_collection_modifyitems из conftest пакета получает тесты всего
прогона, а не только своего каталога. Группу пакет ставит своим тестам
(SuiteItems); тест, которому группу никто не дал, идёт группой своего модуля
(ModuleGroups), чтобы модульные фикстуры поднимались один раз и модули с общей
схемой не мешали друг другу.

Обязательное окружение объявляет метка failif(условие, reason=...) с подписью
skipif: при истинном условии тест не пропускается, а падает с причиной —
прогон без нужного окружения обязан быть красным (Requirements).

Случай, неприменимый к части серверов стенда, получает явный список серверов
(StandServers): неприменимые сочетания не порождаются и в отчёт пропусками не
попадают.

Ошибок наружу нет: невыполненное требование — падение теста.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar, Protocol

import pytest

__all__ = ["ModuleGroups", "Requirements", "StandServers", "SuiteItems"]


class NamedSource(Protocol):
    """Источник стенда: сервер с именем, которым подписан параметр теста."""

    @property
    def name(self) -> str: ...


class StandServers:
    """Явный список серверов теста вместо всех серверов фикстуры."""

    def only(
        self, fixture: str, sources: Sequence[NamedSource]
    ) -> pytest.MarkDecorator:
        """Метка теста: фикстура сервера идёт только по данным источникам.
        Серверы, к которым случай неприменим, не порождаются вовсе."""
        names: list[str] = []
        for source in sources:
            names.append(source.name)

        return pytest.mark.parametrize(fixture, sources, indirect=True, ids=names)


class SuiteItems:
    """Тесты одного набора: всё, что лежит в каталоге его conftest."""

    def __init__(self, conftest: Path) -> None:
        self._root = conftest.resolve().parent

    def own(self, items: Sequence[pytest.Item]) -> list[pytest.Item]:
        chosen: list[pytest.Item] = []
        for item in items:
            if item.path.resolve().is_relative_to(self._root):
                chosen.append(item)

        return chosen


class ModuleGroups:
    """Группа xdist по умолчанию — модуль теста. Хук корневого conftest зовётся
    после хуков пакетов, поэтому группу набора он не перебивает."""

    MARK: ClassVar[str] = "xdist_group"
    SEPARATOR: ClassVar[str] = "::"

    def assign(self, items: Sequence[pytest.Item]) -> None:
        for item in items:
            if item.get_closest_marker(self.MARK) is not None:
                continue

            module = item.nodeid.split(self.SEPARATOR, 1)[0]
            item.add_marker(pytest.mark.xdist_group(module))


class Requirements:
    """Метки failif теста: первое невыполненное требование роняет тест с
    причиной, названной меткой."""

    MARK: ClassVar[str] = "failif"
    REASON: ClassVar[str] = "reason"

    def check(self, item: pytest.Item) -> None:
        for mark in item.iter_markers(name=self.MARK):
            if len(mark.args) != 1 or self.REASON not in mark.kwargs:
                pytest.fail(
                    f"{item.nodeid}: failif expects one condition and reason=..., "
                    f"got args {mark.args} and keys {sorted(mark.kwargs)}",
                    pytrace=False,
                )

            if mark.args[0]:
                pytest.fail(
                    f"{item.nodeid}: required environment is missing: "
                    f"{mark.kwargs[self.REASON]}",
                    pytrace=False,
                )
