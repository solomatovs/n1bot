"""Параллельный прогон стенда насосов под pytest-xdist: метка рабочего
процесса в именах объектов стенда, группы тестов по серверам стенда и общий
для модуля порядок серверов.

Тесты одного модуля на одних серверах идут одной группой: модульные
фикстуры засевают источник один раз, а группы разных пар серверов
расходятся по процессам.
"""

from __future__ import annotations

from enum import IntEnum, nonmember
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
    order = StandOrder(PumpStand.required())
    groups = StandGroups(order)
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

    def place(self, name: str) -> int:
        """Место сервера в своём списке стенда."""
        return self._places[name]


class CrowdedOracle(IntEnum):
    """Выпуски Oracle стенда, чей листенер отбивает параллельные сессии
    (ORA-12516), и на сервер такого выпуска сразу идут не больше LANES групп.
    У 12.2 запас процессов съедают простаивающие PX-процессы экземпляра; у
    Free 23 листенер не успевает за всплеском соединений десятка процессов."""

    ENTERPRISE_12 = 12
    FREE_23 = 23
    LANES = nonmember(3)


class StandGroups:
    """Группа xdist теста: модуль и серверы стенда в его параметрах. Тесты
    одних серверов делят модульные фикстуры и таблицы друг друга, поэтому
    идут одним процессом; имена объектов у процессов свои, и группы разных
    пар серверов, в том числе на одном Oracle, расходятся по процессам.
    Группы тесного Oracle (CrowdedOracle) сведены в LANES дорожек по месту
    второго сервера пары в списке стенда: модули обходят все серверы, и
    дорожки выходят почти равными."""

    SEPARATOR: ClassVar[str] = "::"
    ORACLE: ClassVar[str] = "oracle"

    def __init__(self, order: StandOrder) -> None:
        self._order = order
        self._crowded = frozenset(CrowdedOracle)

    def of(self, item: pytest.Item) -> str:
        module = item.nodeid.split(self.SEPARATOR, 1)[0]
        callspec = getattr(item, "callspec", None)
        if callspec is None:
            return module

        servers: list[str] = [module]
        crowded: list[str] = []
        place = 0
        for value in callspec.params.values():
            if isinstance(value, (PgSource, ChSource, OraSource)):
                servers.append(value.name)

            if isinstance(value, (PgSource, ChSource)):
                place = self._order.place(value.name)
                continue

            if not isinstance(value, OraSource):
                continue

            if value.release in self._crowded:
                crowded.append(value.name)

        if not crowded:
            return self.SEPARATOR.join(servers)

        lane = place % CrowdedOracle.LANES

        return self.SEPARATOR.join((self.ORACLE, *crowded, str(lane)))
