"""Порт сервиса исполнения инструментов: что клиент видит от DAG-сервиса.

Клиент (чат) знает исполнение только через этот порт: список инструментов,
которые сервис отдаёт модели, и вызов инструмента. Один вызов — один запуск
со своим DAG: инструмент без портов — узел сам по себе, связка потоковых
инструментов — вызов инструмента workflow. Реализации порта — исполнитель в
своём процессе (boba.toolrun.stream_calls.LocalDagService) и клиент
MCP-сервера исполнения.

Ошибки: своих не выпускает; отказ вызова едет в CallReply результатом-ошибкой.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from boba.toolkit.result import ToolResult

__all__ = ["CallReply", "CallRequest", "DagService", "DagTool"]


class DagTool(BaseModel):
    """Инструмент сервиса, каким его видит модель: имя, описание и JSON-схема
    аргументов."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    description: str
    input_schema: Mapping[str, Any]


class CallRequest(BaseModel):
    """Вызов инструмента сервиса: идентификатор запуска, инструмент и его
    аргументы в том виде, в каком их назвала модель.

    run_id выдаёт клиент; под ним идут журнал и шаг ленты вызова.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    arguments: Mapping[str, JsonValue] = {}


class CallReply(BaseModel):
    """Итог вызова: текст для модели и результат семейства ToolResultBase.

    errored — вызов кончился ошибкой: отказ до запуска, сбой вызова либо
    сбой хотя бы одного узла workflow.
    """

    model_config = ConfigDict(frozen=True)

    content: str
    artifact: ToolResult
    errored: bool


class DagService(Protocol):
    """Порт сервиса исполнения для клиента."""

    @abstractmethod
    def tools(self) -> Sequence[DagTool]:
        """Инструменты, которые сервис отдаёт модели."""
        ...

    @abstractmethod
    async def call(self, request: CallRequest) -> CallReply:
        """Исполнить вызов и дождаться его итога."""
        ...
