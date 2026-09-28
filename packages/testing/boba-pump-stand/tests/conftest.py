"""Параллельный прогон стенда насосов под pytest-xdist: метка рабочего
процесса в именах объектов стенда и группы тестов по серверам стенда.

Тесты одного модуля на одних серверах идут одной группой: модульные
фикстуры засевают источник один раз, а группы разных пар серверов
расходятся по процессам.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, ClassVar

import pytest

from boba.pump_stand.names import StandNames
from boba.pump_stand.stand import ChSource, OraSource, PgSource


class XdistKey(StrEnum):
    """Ключи xdist в config.workerinput."""

    WORKER = "workerid"


def pytest_configure(config: pytest.Config) -> None:
    """Метка процесса в именах ставится до импорта модулей тестов."""
    workerinput: dict[str, Any] | None = getattr(config, "workerinput", None)
    if workerinput is None:
        return

    StandNames().use_worker(str(workerinput[XdistKey.WORKER]))


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Группы ставятся раньше хука xdist: он читает их в том же событии."""
    groups = StandGroups()
    for item in items:
        item.add_marker(pytest.mark.xdist_group(groups.of(item)))


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
