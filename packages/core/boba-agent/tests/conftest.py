"""Тесты агента на стенде модели сверяют тела своих запросов с золотыми
снимками tests/golden/requests: сборка запроса из истории не меняется
незаметно. Сверяется тест, которому нужна фикстура server модуля и который
прошёл; тесты с недетерминированным составом запросов (гонки отмены,
таймауты) перечислены в UNSTABLE."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import ClassVar

import pytest

from boba.stand.agent import FakeServer
from boba.stand.golden import GoldenOption, GoldenRequests


class GoldenScope:
    """Какие тесты сверяют запросы с эталоном: нужна фикстура server, и тест
    не в списке нестабильных."""

    SERVER: ClassVar[str] = "server"
    ROOT: ClassVar[Path] = Path(__file__).parent / "golden" / "requests"
    UNSTABLE: ClassVar[frozenset[str]] = frozenset()
    """Тесты, у которых число и состав запросов зависят от времени."""
    PASSED: ClassVar[pytest.StashKey[bool]] = pytest.StashKey[bool]()
    """Итог фазы вызова теста в stash."""

    def covers(self, item: pytest.Item) -> bool:
        if not isinstance(item, pytest.Function):
            return False

        if self.SERVER not in item.fixturenames:
            return False

        return item.nodeid.partition("::")[2] not in self.UNSTABLE


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """Итог фазы вызова теста — в item.stash: эталон сверяется только у
    прошедшего теста."""
    report = yield
    if report.when == "call":
        item.stash[GoldenScope.PASSED] = report.passed

    return report


@pytest.fixture(autouse=True)
def golden_requests(request: pytest.FixtureRequest) -> Generator[None, None, None]:
    scope = GoldenScope()
    if not scope.covers(request.node):
        yield
        return

    server = request.getfixturevalue(GoldenScope.SERVER)
    if not isinstance(server, FakeServer):
        msg = f"fixture server of {request.node.nodeid} is not a FakeServer"
        raise TypeError(msg)

    yield

    if not request.node.stash.get(GoldenScope.PASSED, False):
        return

    update = bool(request.config.getoption(GoldenOption.UPDATE.dest))
    GoldenRequests(GoldenScope.ROOT, update).check(
        request.node.nodeid, server.requests()
    )
