"""Общие фикстуры тестов песочницы: том журнала и контекст вызова."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from journal_stand import JournalStand

from boba.sandbox.runner import ToolCallContext


@pytest.fixture(autouse=True)
def journal_stand(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Том журнала и контекст вызова на каждый тест: запуск без них невозможен."""
    JournalStand.use(tmp_path_factory.mktemp("journal"))

    token = ToolCallContext.set(JournalStand.context())
    try:
        yield
    finally:
        ToolCallContext.reset(token)
