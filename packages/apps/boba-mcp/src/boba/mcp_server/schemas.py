"""Слияние карточек MCP с описаниями администратора и проверка контракта.

Ошибки:
EndpointConfigError — override невалиден или меняет контракт инструмента.
ToolError — аргументы не соответствуют итоговой опубликованной схеме.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from copy import deepcopy
from enum import StrEnum
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.tools import Tool
from fastmcp.tools.base import ToolResult
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from mcp.types import Tool as McpTool
from pydantic import PrivateAttr
from pydantic import ValidationError as ModelValidationError

from boba.mcp_server.catalog import EndpointConfigError


class SchemaAnnotation(StrEnum):
    TITLE = "title"
    DESCRIPTION = "description"
    EXAMPLES = "examples"
    COMMENT = "$comment"


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


class CardSchema(StrEnum):
    INPUT = "inputSchema"
    OUTPUT = "outputSchema"


class EndpointSchemas:
    """Готовит единую карточку для публикации и проверки аргументов.

    EndpointTools передаёт исходный Tool и частичный override. Словари
    сливаются рекурсивно, остальные значения заменяются. Меняться могут
    только описания карточки и документационные аннотации JSON Schema.
    """

    def __init__(self) -> None:
        self._annotations = frozenset(SchemaAnnotation)
        self._children = frozenset(SchemaChildren)
        self._branches = frozenset(SchemaBranch)

    def export(self, tools: Mapping[str, Tool]) -> Mapping[str, Mapping[str, object]]:
        """Полные карточки в том же формате, который принимает overrides."""
        exported: dict[str, Mapping[str, object]] = {}
        for name, tool in tools.items():
            exported[name] = self._card(tool)

        return exported

    def effective(
        self, endpoint: str, tool: Tool, override: Mapping[str, Any]
    ) -> McpTool:
        label = f"endpoint {endpoint!r}, tool {tool.name!r}"
        source = self._card(tool)
        merged = self._merge(source, override)
        try:
            card = McpTool.model_validate(merged, strict=True)
            for key in CardSchema:
                if key in merged:
                    Draft202012Validator.check_schema(merged[key])
        except (SchemaError, ModelValidationError) as exc:
            raise EndpointConfigError(f"{label}: invalid override: {exc}") from exc

        original_contract = self._card_contract(source)
        merged_contract = self._card_contract(merged)
        differences = self._differences(original_contract, merged_contract, "")
        difference = next(differences, None)
        if difference is not None:
            raise EndpointConfigError(
                f"{label}: override changes protected field {difference}; "
                "argument names, types, required fields, defaults, constraints "
                "and tool metadata must remain unchanged; only descriptions, "
                "titles and schema examples/$comment may change"
            )

        return card

    @staticmethod
    def _card(tool: Tool) -> dict[str, Any]:
        return tool.to_mcp_tool().model_dump(
            mode="json", by_alias=True, exclude_none=True
        )

    def _merge(
        self, source: Mapping[str, Any], override: Mapping[str, Any]
    ) -> dict[str, Any]:
        merged = deepcopy(dict(source))
        for key, value in override.items():
            current = merged.get(key)
            if not isinstance(current, dict):
                merged[key] = deepcopy(value)
                continue

            if not isinstance(value, dict):
                merged[key] = deepcopy(value)
                continue

            merged[key] = self._merge(current, value)

        return merged

    def _card_contract(self, card: Mapping[str, Any]) -> dict[str, Any]:
        result = deepcopy(dict(card))
        result.pop("description", None)
        result.pop("title", None)
        for key in CardSchema:
            if key in result:
                result[key] = self._schema_contract(result[key])

        annotations = result.get("annotations")
        if isinstance(annotations, dict):
            annotations.pop("title", None)
            if not annotations:
                result.pop("annotations")

        return result

    def _schema_contract(self, schema: Any) -> Any:
        if isinstance(schema, list):
            return [self._schema_contract(item) for item in schema]

        if not isinstance(schema, dict):
            return schema

        result: dict[str, Any] = {}
        for key, value in schema.items():
            if key in self._annotations:
                continue

            if key in self._children:
                result[key] = {
                    name: self._schema_contract(child) for name, child in value.items()
                }
                continue

            if key in self._branches:
                result[key] = self._schema_contract(value)
                continue

            result[key] = value

        return result

    def _differences(self, source: Any, merged: Any, path: str) -> Iterator[str]:
        if type(source) is not type(merged):
            yield path
            return

        if isinstance(source, dict):
            for key in sorted(source.keys() | merged.keys()):
                field = str(key).replace("~", "~0").replace("/", "~1")
                location = f"{path}/{field}"
                if key not in source:
                    yield location
                    continue

                if key not in merged:
                    yield location
                    continue

                yield from self._differences(source[key], merged[key], location)

            return

        if isinstance(source, list):
            if len(source) != len(merged):
                yield path
                return

            pairs = zip(source, merged, strict=True)
            for index, (original, changed) in enumerate(pairs):
                yield from self._differences(original, changed, f"{path}/{index}")

            return

        if source != merged:
            yield path


class DescribedTool(Tool):
    """Исполняет исходный инструмент с единой провалидированной карточкой MCP."""

    _original: Tool = PrivateAttr()
    _validator: Draft202012Validator = PrivateAttr()

    def __init__(self, original: Tool, card: McpTool) -> None:
        values = original.model_dump()
        values.update(
            name=card.name,
            title=card.title,
            description=card.description,
            parameters=card.input_schema,
            output_schema=card.output_schema,
            annotations=card.annotations,
            icons=card.icons,
            execution=card.execution,
            meta=card.meta,
        )
        super().__init__(**values)
        self._original = original
        self._validator = Draft202012Validator(self.parameters)

    def to_mcp_tool(self, **overrides: Any) -> McpTool:
        overrides.setdefault("title", self.title)
        return super().to_mcp_tool(**overrides)

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        try:
            self._validator.validate(arguments)
        except ValidationError as exc:
            path = "/".join(map(str, exc.absolute_path))
            raise ToolError(
                f"tool {self.name!r}: arguments at /{path} violate {exc.validator!r}"
            ) from exc

        return await self._original.run(arguments)
