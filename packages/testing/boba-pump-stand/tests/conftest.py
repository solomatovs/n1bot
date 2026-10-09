"""Параллельный прогон стенда насосов под pytest-xdist: метка рабочего
процесса в именах объектов стенда, группы тестов по серверам стенда и общий
для модуля порядок серверов.

Тесты одного модуля на одних серверах идут одной группой: модульные
фикстуры засевают источник один раз, а группы разных пар серверов
расходятся по процессам.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from boba.pump_stand.stand import ChSource, OraSource, PgSource, PumpStand
from boba.stand.collection import SuiteItems
from boba.stand.names import StandSuite


def pytest_configure(config: pytest.Config) -> None:
    """Метка набора и процесса в именах ставится до импорта модулей тестов."""
    StandSuite(config).configure("pump")


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Группы ставятся раньше хука xdist: он читает их в том же событии. Хук
    получает тесты всего прогона, группы набора идут только его тестам."""
    groups = StandGroups()
    order = StandOrder(PumpStand.required())
    for item in SuiteItems(Path(__file__)).own(items):
        order.align(item)
        item.add_marker(pytest.mark.xdist_group(groups.of(item)))


class StandOrder:
    """Номер сервера в параметрах теста — его место в списке стенда, какой бы
    список ни дал тесту серверы. pytest собирает тесты модульной фикстуры
    вместе по номеру параметра; у теста с явным списком серверов
    (PumpStand.only) счёт шёл бы по этому списку, тест попадал бы к чужому
    серверу, и фикстуры сторон пересоздавались бы посреди сценария модуля,
    снося таблицы предыдущих тестов."""

    def __init__(self, stand: PumpStand) -> None:
        self._places: dict[str, int] = {}
        for sources in (stand.sources, stand.ch_sources, stand.ora_sources):
            for place, source in enumerate(sources):
                self._places[source.name] = place

    def align(self, item: pytest.Item) -> None:
        callspec = getattr(item, "callspec", None)
        if callspec is None:
            return

        for argname, value in callspec.params.items():
            if not isinstance(value, (PgSource, ChSource, OraSource)):
                continue

            if value.name not in self._places:
                continue

            callspec.indices[argname] = self._places[value.name]


class StandGroups:
    """Группа xdist теста: модуль и серверы стенда в его параметрах. Тесты
    одних серверов делят модульные фикстуры и таблицы друг друга, поэтому
    идут одним процессом. Всё, что трогает Oracle, идёт одной группой на
    сервер: параллельные сессии Oracle 12.2 стенда отбивает листенер."""

    SEPARATOR: ClassVar[str] = "::"
    ORACLE: ClassVar[str] = "oracle"

    def of(self, item: pytest.Item) -> str:
        module = item.nodeid.split(self.SEPARATOR, 1)[0]
        callspec = getattr(item, "callspec", None)
        if callspec is None:
            return module

        servers: list[str] = [module]
        for value in callspec.params.values():
            if isinstance(value, OraSource):
                return f"{self.ORACLE}{self.SEPARATOR}{value.name}"

            if isinstance(value, (PgSource, ChSource)):
                servers.append(value.name)

        return self.SEPARATOR.join(servers)
