"""Фикстуры пакета: изоляция от chainlit-контекста общего conftest'а."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from boba.sandbox.runner import ToolCallContext
from boba.stand.journal import CallStand


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


@pytest.fixture(autouse=True)
def tool_call_context() -> Iterator[ToolCallContext]:
    """Адрес вызова для журнала: песочница без контекста не запускается."""
    with CallStand.bound() as context:
        yield context
