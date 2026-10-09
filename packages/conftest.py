"""Общие фикстуры прогонов: конфиг приложения, стенд и anyio-бэкенд."""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from typing import ClassVar

import pytest

from boba.stand.collection import ModuleGroups, Requirements
from boba.stand.names import StandSuite
from boba.stand.site import Stand

pytest_plugins = ["boba.stand.fixtures", "boba.stand.ui.fixtures"]


class ChainlitTestRoot:
    """Корень chainlit процесса pytest и его подпроцессов.

    chainlit на импорте заводит под APP_ROOT каталоги .chainlit и .files, а без
    переменной берёт рабочий каталог — тесты оставляли их там, откуда запущен
    pytest. Корень прогона — временный каталог: его называет переменная до
    первого импорта chainlit, а конец процесса каталог удаляет.
    """

    ENV: ClassVar[str] = "CHAINLIT_APP_ROOT"
    PREFIX: ClassVar[str] = "boba-chainlit-root-"

    def install(self) -> None:
        root = tempfile.mkdtemp(prefix=self.PREFIX)
        os.environ[self.ENV] = root
        atexit.register(shutil.rmtree, root, ignore_errors=True)


ChainlitTestRoot().install()


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
