"""Окружение сервиса: свои версии библиотек и общий код проекта рядом с ними."""

from __future__ import annotations

from importlib.metadata import version

from packaging.version import Version

from boba.toolrun.stream_calls import ToolServer


class TestServiceEnvironment:
    def test_service_runs_on_the_libraries_the_chat_cannot_take(self) -> None:
        """Чат держит pydantic 2.10 и mcp 1.x (chainlit); сервису нужны
        fastmcp 4 на mcp 2.x и pydantic не ниже 2.12."""
        if Version(version("fastmcp")).major != 4:
            raise AssertionError(f"fastmcp 4.x: {version('fastmcp')}")
        if Version(version("mcp")).major != 2:
            raise AssertionError(f"mcp 2.x: {version('mcp')}")
        if Version(version("pydantic")) < Version("2.12"):
            raise AssertionError(f"pydantic не ниже 2.12: {version('pydantic')}")

    def test_shared_code_is_importable(self) -> None:
        if not hasattr(ToolServer, "submit"):
            raise AssertionError("порт сервера инструментов виден из окружения сервиса")
