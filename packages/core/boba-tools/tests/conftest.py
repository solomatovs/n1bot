"""Pytest-фикстуры пакета boba-tools.

NB: в существующих тестах `_SOURCE = ToolSourceId("test")` и
`_ctx() -> ToolContext()` оставлены как module-level constants —
они тривиальны и не требуют setup/teardown. Эти фикстуры —
для новых тестов, чтобы единый паттерн.
"""

from __future__ import annotations

import pytest

from boba.tools.domain import ToolContext, ToolSourceId


@pytest.fixture
def tool_ctx() -> ToolContext:
    """Пустой `ToolContext` для tool.stream(...) вызовов."""
    return ToolContext()


@pytest.fixture
def tool_source_id() -> ToolSourceId:
    """Тестовый `ToolSourceId('test')`."""
    return ToolSourceId("test")
