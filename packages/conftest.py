"""Общие фикстуры прогонов: конфиг приложения, стенд и anyio-бэкенд."""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from enum import StrEnum
from typing import ClassVar

import pytest

from boba.stand.collection import ModuleGroups, Requirements
from boba.stand.golden import GoldenOption
from boba.stand.names import StandSuite
from boba.stand.site import Stand

pytest_plugins = ["boba.stand.fixtures", "boba.stand.ui.fixtures"]
collect_ignore = [
    "services/boba-catalog-service",
    "core/boba-catalog",
]


class ChainlitTestRoot:
    """Корень chainlit процесса pytest и его подпроцессов.

    chainlit на импорте заводит под APP_ROOT каталоги .chainlit и .files, а без
    переменной берёт рабочий каталог — тесты оставляли их там, откуда запущен
    pytest. Корень прогона — временный каталог: его называет переменная до
    первого импорта chainlit, а конец процесса каталог удаляет.
    """

    ENV: ClassVar[str] = "CHAINLIT_APP_ROOT"
    PREFIX: ClassVar[str] = "boba-aid-root-"

    def install(self) -> None:
        root = tempfile.mkdtemp(prefix=self.PREFIX)
        os.environ[self.ENV] = root
        atexit.register(shutil.rmtree, root, ignore_errors=True)


ChainlitTestRoot().install()


class LookOption(StrEnum):
    """Опции тестов вида aid (packages/apps/boba-aid/tests/look): каталог
    эталонов, переснять эталоны и каталог отчёта. Объявлены здесь, чтобы
    прогон любого корня тестов их знал."""

    BASELINE = "--look-baseline"
    UPDATE = "--look-update"
    OUT = "--look-out"

    @property
    def dest(self) -> str:
        return self.value.removeprefix("--").replace("-", "_")


def pytest_addoption(parser: pytest.Parser) -> None:
    golden = parser.getgroup("golden", "золотые снимки запросов к модели")
    golden.addoption(
        GoldenOption.UPDATE.value,
        dest=GoldenOption.UPDATE.dest,
        action="store_true",
        default=False,
        help=(
            "переснять эталоны запросов тестов агента; прогон всё равно падает, "
            "эталоны принимаются git"
        ),
    )
    group = parser.getgroup("look", "тесты вида aid")
    group.addoption(
        LookOption.UPDATE.value,
        dest=LookOption.UPDATE.dest,
        nargs="?",
        const="*",
        default="",
        help=(
            "переснять эталоны тестов вида, имя которых подходит под маску "
            "(без маски — все); прогон всё равно падает, новые эталоны принимаются "
            "просмотром отчёта"
        ),
    )
    group.addoption(
        LookOption.BASELINE.value,
        dest=LookOption.BASELINE.dest,
        default="",
        help="каталог эталонов тестов вида вне git; без него тесты вида не идут",
    )
    group.addoption(
        LookOption.OUT.value,
        dest=LookOption.OUT.dest,
        default="",
        help="каталог отчёта тестов вида; по умолчанию debug/look-out корня",
    )


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    """Метка пакета в именах стенда; conftest набора задаёт свою после неё:
    без tryfirst pluggy зовёт корневой хук последним, и он затирал бы её."""
    StandSuite(config).configure_package(config.args)


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Тест без группы набора получает группу своего модуля; раньше хука
    xdist: он читает группы в том же событии."""
    ModuleGroups().assign(items)


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Метка failif: без обязательного окружения тест падает с причиной."""
    Requirements().check(item)


@pytest.fixture(scope="session")
def stand() -> Stand:
    """Адреса, принципалы и учётки стенда: в коде тестов их быть не должно."""
    return Stand.load()


@pytest.fixture(scope="session")
def live_kdc(stand: Stand) -> None:
    """Локальный AD обязателен: без keytab и krb5.conf тест падает с причиной."""
    if stand.live():
        return

    pytest.fail("нет keytab/krb5.conf локального AD")
