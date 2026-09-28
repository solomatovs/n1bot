"""Фикстуры тестов chainlit: определены в chainlit_stand, здесь регистрация, метка
набора в именах стенда и группы pytest-xdist."""

from enum import StrEnum
from typing import ClassVar

import pytest
from chainlit_stand import *  # noqa: F403 — модуль стенда и есть набор фикстур

from boba.stand.names import StandSuite


def pytest_configure(config: pytest.Config) -> None:
    """Метка набора и процесса в именах стенда ставится до импорта модулей тестов."""
    StandSuite(config).configure("chainlit")


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Группы ставятся раньше хука xdist: он читает их в том же событии."""
    groups = ChainlitGroups()
    for item in items:
        item.add_marker(pytest.mark.xdist_group(groups.of(item)))


class ChainlitGroup(StrEnum):
    """Группы xdist, общие для нескольких модулей."""

    E2E = "e2e"


class ChainlitGroups:
    """Группа xdist теста chainlit: модуль целиком, чтобы модульные фикстуры
    поднимались один раз. e2e-модули запускают настоящее приложение с общим
    BOBA_INSTANCE_ID и портом BOBA_E2E_PORT и идут одним процессом по очереди."""

    E2E_MODULES: ClassVar[frozenset[str]] = frozenset(
        {"test_canvas_e2e.py", "test_stream_e2e.py"}
    )
    SEPARATOR: ClassVar[str] = "::"

    def of(self, item: pytest.Item) -> str:
        if item.path.name in self.E2E_MODULES:
            return ChainlitGroup.E2E.value

        return item.nodeid.split(self.SEPARATOR, 1)[0]
