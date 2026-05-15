"""Pydantic-адаптеры для типов из чужих пакетов.

Эти `Annotated`-типы делают возможным описание Pydantic-моделей
(в первую очередь `AgentEvent` после миграции), у которых поля имеют
типы из пакетов, которые мы трогать не хотим/не можем:

- `boba.patterns.Id` (`RequestId`) — глобально менять `Id` опасно
  (десятки потребителей в indexing/data_layer).
- `boba.llm.models.ToolCall`, `InvalidToolCall` — чужой пакет.
- `boba.tools.domain.ToolResult` (sealed: text/json/error) — чужой пакет.

Каждый адаптер оформлен как:

    Annotated[ConcreteType,
              PlainValidator(_validate_X),    # str/dict → instance
              PlainSerializer(_serialize_X),   # instance → str/dict
              WithJsonSchema(...)]             # явная схема для OpenAPI

`_validate_X` принимает либо готовый instance (passthrough — важно
для сценария "DTO собран в коде, потом провалидирован"), либо
wire-форму (str/dict из JSON), и конструирует instance через
существующие классовые конструкторы.

Используем `PlainValidator`, а не `BeforeValidator`: первый полностью
заменяет inner-schema, поэтому Pydantic не пытается генерировать
схему для произвольного класса (`RequestId`, `ToolCall`, ...) — мы
полностью отвечаем за его (де)валидацию.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any

from pydantic import PlainSerializer, PlainValidator, WithJsonSchema

from boba.llm.models import InvalidToolCall, RequestId, ToolCall
from boba.tools.domain import ErrorResult, JsonResult, TextResult, ToolResult

__all__ = [
    "InvalidToolCallField",
    "RequestIdField",
    "ToolCallField",
    "ToolResultField",
]


# --------------------------------------------------------------------- #
# RequestId
# --------------------------------------------------------------------- #


def _validate_request_id(value: Any) -> RequestId:
    if isinstance(value, RequestId):
        return value
    if isinstance(value, str):
        return RequestId.from_wire(value)
    msg = (
        f"RequestId: ожидается str или RequestId, "
        f"получено {type(value).__name__}"
    )
    raise TypeError(msg)


def _serialize_request_id(value: RequestId) -> str:
    return value.to_wire()


RequestIdField = Annotated[
    RequestId,
    PlainValidator(_validate_request_id),
    PlainSerializer(_serialize_request_id, return_type=str, when_used="always"),
    WithJsonSchema({"type": "string", "format": "uuid"}),
]


# --------------------------------------------------------------------- #
# ToolCall
# --------------------------------------------------------------------- #


def _validate_tool_call(value: Any) -> ToolCall:
    if isinstance(value, ToolCall):
        return value
    if isinstance(value, Mapping):
        return ToolCall(id=value["id"], name=value["name"], args=value["args"])
    msg = (
        f"ToolCall: ожидается Mapping или ToolCall, "
        f"получено {type(value).__name__}"
    )
    raise TypeError(msg)


def _serialize_tool_call(value: ToolCall) -> dict[str, Any]:
    return {"id": value.id, "name": value.name, "args": dict(value.args)}


ToolCallField = Annotated[
    ToolCall,
    PlainValidator(_validate_tool_call),
    PlainSerializer(_serialize_tool_call, return_type=dict, when_used="always"),
    WithJsonSchema(
        {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "name": {"type": "string"},
                "args": {"type": "object"},
            },
            "required": ["id", "name", "args"],
        },
    ),
]


# --------------------------------------------------------------------- #
# InvalidToolCall
# --------------------------------------------------------------------- #


def _validate_invalid_tool_call(value: Any) -> InvalidToolCall:
    if isinstance(value, InvalidToolCall):
        return value
    if isinstance(value, Mapping):
        return InvalidToolCall(
            id=value["id"],
            name=value["name"],
            raw_args=value["raw_args"],
            error=value["error"],
        )
    msg = (
        f"InvalidToolCall: ожидается Mapping или InvalidToolCall, "
        f"получено {type(value).__name__}"
    )
    raise TypeError(msg)


def _serialize_invalid_tool_call(value: InvalidToolCall) -> dict[str, Any]:
    return {
        "id": value.id,
        "name": value.name,
        "raw_args": value.raw_args,
        "error": value.error,
    }


InvalidToolCallField = Annotated[
    InvalidToolCall,
    PlainValidator(_validate_invalid_tool_call),
    PlainSerializer(
        _serialize_invalid_tool_call, return_type=dict, when_used="always",
    ),
    WithJsonSchema(
        {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "name": {"type": "string"},
                "raw_args": {"type": "string"},
                "error": {"type": "string"},
            },
            "required": ["id", "name", "raw_args", "error"],
        },
    ),
]


# --------------------------------------------------------------------- #
# ToolResult (sealed: text / json / error — tagged union по `kind`)
# --------------------------------------------------------------------- #


def _validate_tool_result(value: Any) -> ToolResult:
    if isinstance(value, ToolResult):
        return value
    if not isinstance(value, Mapping):
        msg = (
            f"ToolResult: ожидается Mapping или ToolResult, "
            f"получено {type(value).__name__}"
        )
        raise TypeError(msg)
    kind = value.get("kind")
    metadata = value.get("metadata", {})
    if kind == "text":
        return TextResult(text=value["text"], metadata=metadata)
    if kind == "json":
        return JsonResult(payload=value["payload"], metadata=metadata)
    if kind == "error":
        return ErrorResult(
            message=value["message"],
            error_kind=value["error_kind"],
            metadata=metadata,
        )
    msg = f"ToolResult: неизвестный kind={kind!r}"
    raise ValueError(msg)


def _serialize_tool_result(value: ToolResult) -> dict[str, Any]:
    if isinstance(value, TextResult):
        return {
            "kind": "text",
            "text": value.text,
            "metadata": dict(value.metadata),
        }
    if isinstance(value, JsonResult):
        return {
            "kind": "json",
            "payload": value.payload,
            "metadata": dict(value.metadata),
        }
    if isinstance(value, ErrorResult):
        return {
            "kind": "error",
            "message": value.message,
            "error_kind": value.error_kind,
            "metadata": dict(value.metadata),
        }
    msg = f"ToolResult: неизвестный подтип {type(value).__name__}"
    raise TypeError(msg)


ToolResultField = Annotated[
    ToolResult,
    PlainValidator(_validate_tool_result),
    PlainSerializer(_serialize_tool_result, return_type=dict, when_used="always"),
    WithJsonSchema(
        {
            "oneOf": [
                {
                    "type": "object",
                    "properties": {
                        "kind": {"const": "text"},
                        "text": {"type": "string"},
                        "metadata": {"type": "object"},
                    },
                    "required": ["kind", "text"],
                },
                {
                    "type": "object",
                    "properties": {
                        "kind": {"const": "json"},
                        "payload": {},
                        "metadata": {"type": "object"},
                    },
                    "required": ["kind", "payload"],
                },
                {
                    "type": "object",
                    "properties": {
                        "kind": {"const": "error"},
                        "message": {"type": "string"},
                        "error_kind": {"type": "string"},
                        "metadata": {"type": "object"},
                    },
                    "required": ["kind", "message", "error_kind"],
                },
            ],
            "discriminator": {"propertyName": "kind"},
        },
    ),
]
