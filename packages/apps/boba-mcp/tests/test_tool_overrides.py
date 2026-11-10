"""Карточки настоящего MCP Tool: слияние и защита вложенного JSON Schema."""

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

import pytest
from fastmcp.tools import Tool

from boba.mcp_server.catalog import EndpointConfigError
from boba.mcp_server.schemas import DescribedTool, EndpointSchemas
from boba.mcp_server.server import FileUploadSchema


@pytest.mark.parametrize(
    ("override", "path"),
    [
        ({"name": "renamed"}, "/name"),
        ({"inputSchema": {"properties": {"unknown": {}}}}, "/properties/unknown"),
        ({"inputSchema": {"properties": {"name": {"type": "integer"}}}}, "/name/type"),
        (
            {"inputSchema": {"properties": {"name": {"minLength": 2}}}},
            "/name/minLength",
        ),
        ({"inputSchema": {"properties": {"name": {"default": "x"}}}}, "/name/default"),
        ({"inputSchema": {"required": []}}, "/required"),
        ({"inputSchema": {"additionalProperties": False}}, "/additionalProperties"),
        (
            {"inputSchema": {"properties": {"name": {"descrption": "typo"}}}},
            "/descrption",
        ),
        ({"annotations": {"readOnlyHint": True}}, "/annotations"),
        ({"_meta": {"ui": {"visibility": ["model"]}}}, "/_meta/ui/visibility/0"),
        ({"unexpected": "value"}, "/unexpected"),
        ({"properties": {"name": {"description": "legacy"}}}, "/properties"),
        (
            {"inputSchema": {"properties": {"scope_id": {"type": "integer"}}}},
            "/scope_id",
        ),
        ({"description": 42}, "invalid override"),
        (
            {"inputSchema": {"properties": {"name": {"description": 42}}}},
            "invalid override",
        ),
    ],
)
def test_tool_card_rejects_executable_changes_and_invalid_documentation(
    override: Mapping[str, Any], path: str
) -> None:
    with pytest.raises(EndpointConfigError) as caught:
        EndpointSchemas().effective("/files", FileUploadSchema().tool, override)

    assert "endpoint '/files', tool 'file_upload'" in str(caught.value)
    assert path in str(caught.value)


@pytest.mark.parametrize("title", ["Document upload", "", None])
def test_full_card_roundtrip_preserves_explicit_documentation(
    title: str | None,
) -> None:
    original = FileUploadSchema().tool
    schemas = EndpointSchemas()
    exported = dict(schemas.export({original.name: original})[original.name])
    exported["title"] = title
    exported["description"] = "Revised attachment description"
    merged = schemas.effective("/files", original, exported)
    wrapped = DescribedTool(original, merged)
    assert wrapped.to_mcp_tool() == merged
    assert wrapped.parameters == merged.input_schema


class NestedToolCard:
    """Контракт с полем description, ссылкой, массивом и вариантами значений."""

    def __init__(self) -> None:
        self.tool = Tool(
            name="nested",
            description="Original",
            parameters={
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "records": {"type": "array", "items": {"$ref": "#/$defs/record"}},
                },
                "$defs": {
                    "record": {
                        "type": "object",
                        "properties": {
                            "value": {
                                "anyOf": [{"type": "string"}, {"type": "null"}],
                                "default": None,
                            }
                        },
                    }
                },
            },
            output_schema={"type": "object", "properties": {"ok": {"const": True}}},
        )
        self.schemas = EndpointSchemas()


def test_nested_annotations_merge_without_losing_contract_or_mutating_source() -> None:
    case = NestedToolCard()
    original = deepcopy(case.tool.parameters)
    override = {
        "inputSchema": {
            "properties": {"description": {"description": "A field named description"}},
            "$defs": {
                "record": {
                    "properties": {
                        "value": {
                            "anyOf": [
                                {"type": "string", "description": "A value"},
                                {"type": "null", "description": "Absent"},
                            ],
                        }
                    }
                }
            },
        },
        "outputSchema": {"properties": {"ok": {"description": "Completed"}}},
    }
    saved = deepcopy(override)
    card = case.schemas.effective("/nested", case.tool, override)
    assert card.input_schema["properties"]["description"]["type"] == "string"
    assert (
        card.input_schema["properties"]["records"] == original["properties"]["records"]
    )
    field = card.input_schema["$defs"]["record"]["properties"]["value"]
    assert field["default"] is None
    assert field["anyOf"][0]["description"] == "A value"
    assert card.output_schema is not None
    assert card.output_schema["properties"]["ok"] == {
        "const": True,
        "description": "Completed",
    }
    assert case.tool.parameters == original
    assert override == saved


@pytest.mark.parametrize(
    "override",
    [
        {"inputSchema": {"properties": {"description": {"type": "integer"}}}},
        {
            "inputSchema": {
                "$defs": {
                    "record": {
                        "properties": {
                            "value": {
                                "anyOf": [{"type": "number"}, {"type": "null"}],
                            }
                        }
                    }
                }
            }
        },
        {"outputSchema": {"properties": {"ok": {"const": 1}}}},
    ],
)
def test_nested_contract_changes_are_rejected(override: Mapping[str, Any]) -> None:
    case = NestedToolCard()
    with pytest.raises(EndpointConfigError, match="protected field"):
        case.schemas.effective("/nested", case.tool, override)
