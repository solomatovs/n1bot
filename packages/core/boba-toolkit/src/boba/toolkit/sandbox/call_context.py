"""Имя инструмента текущего вызова песочницы; langchain копирует contextvars
в executor-поток, поэтому значение видно и синхронному SandboxRunner."""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import ClassVar

__all__ = ["ToolCallContext"]


class ToolCallContext:
    """Имя langchain-инструмента в текущем контексте выполнения."""

    _name: ClassVar[ContextVar[str]] = ContextVar("tool_call_name", default="")

    @classmethod
    def set(cls, name: str) -> Token[str]:
        return cls._name.set(name)

    @classmethod
    def reset(cls, token: Token[str]) -> None:
        cls._name.reset(token)

    @classmethod
    def get(cls) -> str:
        return cls._name.get()
