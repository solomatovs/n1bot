"""Переопределение описаний схем без изменения контракта исполнителя.

Ошибки:
EndpointConfigError — схема endpoint'а невалидна или меняет контракт плагина.
ToolError — аргументы не соответствуют опубликованной схеме; fastmcp
    переводит отказ в tools/call с isError.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from enum import StrEnum
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.tools import Tool
from fastmcp.tools.base import ToolResult
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from pydantic import PrivateAttr

from boba.mcp_server.catalog import EndpointConfigError
from boba.toolrun.call_id import CallFields


class SchemaAnnotation(StrEnum):
    TITLE = "title"
    DESCRIPTION = "description"
    EXAMPLES = "examples"
    COMMENT = "$comment"
    DEPRECATED = "deprecated"
    READ_ONLY = "readOnly"
    WRITE_ONLY = "writeOnly"


class SchemaChildren(StrEnum):
    PROPERTIES = "properties"
    PATTERNS = "patternProperties"
    DEFINITIONS = "$defs"
    LEGACY_DEFINITIONS = "definitions"
    DEPENDENT = "dependentSchemas"


class SchemaBranch(StrEnum):
    ITEMS = "items"
    ADDITIONAL = "additionalProperties"
    UNEVALUATED = "unevaluatedProperties"
    UNEVALUATED_ITEMS = "unevaluatedItems"
    CONTAINS = "contains"
    NAMES = "propertyNames"
    NOT = "not"
    IF = "if"
    THEN = "then"
    ELSE = "else"
    ALL = "allOf"
    ANY = "anyOf"
    ONE = "oneOf"
    PREFIX = "prefixItems"


class EndpointSchemas:
    """Проверяет полный inputSchema и возвращает схему с серверными полями scope.

    Сборка endpoint'а сравнивает override с карточкой runtime. Разрешены только
    аннотации: валидирующая часть и служебные расширения обязаны совпадать.
    """

    def __init__(self) -> None:
        self._scope = CallFields().feature()
        self._annotations = frozenset(SchemaAnnotation)
        self._children = frozenset(SchemaChildren)
        self._branches = frozenset(SchemaBranch)

    def model_schema(self, source: Mapping[str, Any]) -> dict[str, Any]:
        result = deepcopy(dict(source))
        hidden = self._scope.params(source)
        properties = result.get("properties", {})
        for name in hidden:
            properties.pop(name, None)

        if "required" in result:
            result["required"] = [
                name for name in result["required"] if name not in hidden
            ]

        return result

    def effective(
        self, endpoint: str, tool: Tool, override: Mapping[str, Any]
    ) -> dict[str, Any]:
        label = f"endpoint {endpoint!r}, tool {tool.name!r}"
        schema = deepcopy(dict(override))
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise EndpointConfigError(
                f"{label}: invalid inputSchema: {exc.message}"
            ) from exc

        if schema.get("type") != "object":
            raise EndpointConfigError(
                f"{label}: inputSchema root must have type object"
            )

        source = tool.parameters
        hidden = self._scope.params(source)
        properties = schema.get("properties", {})
        for name in hidden:
            if name in properties:
                raise EndpointConfigError(
                    f"{label}: {name!r} is server-managed; omit it from overrides"
                )

        base = self.model_schema(source)
        if self._contract(base) != self._contract(schema):
            raise EndpointConfigError(
                f"{label}: override changes the executable argument contract; "
                "copy the exported model schema and change only descriptions, "
                "titles, examples or other annotations"
            )

        return self._with_scope(schema, source, hidden)

    def _with_scope(
        self,
        schema: dict[str, Any],
        source: Mapping[str, Any],
        hidden: Mapping[str, object],
    ) -> dict[str, Any]:
        if hidden:
            properties = schema.setdefault("properties", {})
            for name in hidden:
                properties[name] = deepcopy(source["properties"][name])

            required = schema.setdefault("required", [])
            for name in source.get("required", []):
                if name in hidden:
                    required.append(name)

            if not required:
                schema.pop("required", None)

        return schema

    def _contract(self, schema: Any) -> Any:
        if isinstance(schema, list):
            return [self._contract(item) for item in schema]

        if not isinstance(schema, dict):
            return schema

        result: dict[str, Any] = {}
        for key, value in schema.items():
            if key in self._annotations:
                continue

            if key in self._children:
                result[key] = {
                    name: self._contract(child) for name, child in value.items()
                }
                continue

            if key in self._branches:
                result[key] = self._contract(value)
                continue

            result[key] = value

        return result


class DescribedTool(Tool):
    """Проверяет аргументы по схеме endpoint'а и делегирует вызов исходному Tool."""

    _original: Tool = PrivateAttr()
    _validator: Draft202012Validator = PrivateAttr()

    def __init__(self, original: Tool, schema: Mapping[str, Any]) -> None:
        values = original.model_dump()
        values["parameters"] = dict(schema)
        super().__init__(**values)
        self._original = original
        self._validator = Draft202012Validator(schema)

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        try:
            self._validator.validate(arguments)
        except ValidationError as exc:
            path = "/".join(map(str, exc.absolute_path))
            raise ToolError(
                f"tool {self.name!r}: arguments at /{path} violate {exc.validator!r}"
            ) from exc

        return await self._original.run(arguments)
