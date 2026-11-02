"""Карточки и ответы протокола MCP на границе общей модели."""

from __future__ import annotations

import mcp_types as mt
import pytest
from mcp_types.jsonrpc import ErrorData

from boba.mcp_models import ToolHints
from boba.mcp_models.protocol import ToolCards, ToolReplies


@pytest.mark.parametrize(
    ("annotations", "expected"),
    [
        (None, ToolHints()),
        (mt.ToolAnnotations(), ToolHints()),
        (mt.ToolAnnotations(read_only_hint=True), ToolHints(read_only=True)),
        (
            mt.ToolAnnotations(destructive_hint=False),
            ToolHints(destructive=False),
        ),
        (
            mt.ToolAnnotations(read_only_hint=True, destructive_hint=False),
            ToolHints(read_only=True, destructive=False),
        ),
    ],
)
def test_annotations_keep_conservative_defaults(
    annotations: mt.ToolAnnotations | None, expected: ToolHints
) -> None:
    tool = mt.Tool(
        name="count", input_schema={"type": "object"}, annotations=annotations
    )
    cards = ToolCards()

    card = cards.card("warehouse", tool)

    assert card.name == "mcp__warehouse__count"
    assert card.description == "count"
    assert card.parameters == {"type": "object"}
    assert card.hints == expected
    restored = mt.Tool(
        name=tool.name,
        input_schema=tool.input_schema,
        annotations=cards.annotations_of(card.hints),
    )
    assert cards.hints_of(restored) == expected


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (mt.TextContent(text="visible"), "visible"),
        (
            mt.TextContent(
                text="private", annotations=mt.Annotations(audience=["user"])
            ),
            '{\n  "count": 5\n}',
        ),
        (
            mt.TextContent(
                text="model", annotations=mt.Annotations(audience=["assistant"])
            ),
            "model",
        ),
        (
            mt.TextContent(text="hidden", annotations=mt.Annotations(audience=[])),
            '{\n  "count": 5\n}',
        ),
        (
            mt.ResourceLink(uri="data://report", name="report", mime_type="text/plain"),
            "[resource_link report text/plain]",
        ),
        (
            mt.EmbeddedResource(
                resource=mt.TextResourceContents(
                    uri="data://report", mime_type="text/plain", text="embedded"
                )
            ),
            "[resource data://report text/plain]",
        ),
        (mt.ImageContent(data="AA==", mime_type="image/png"), "[image image/png]"),
    ],
)
def test_model_text_respects_audience_and_structured_fallback(
    content: mt.ContentBlock, expected: str
) -> None:
    result = mt.CallToolResult(content=[content], structured_content={"count": 5})

    assert ToolReplies().result_text(result, "count", "call-1") == expected


def test_raw_reply_keeps_unknown_fields_and_wire_aliases() -> None:
    raw = {
        "content": [{"type": "text", "text": "пять"}],
        "structuredContent": {"count": 5},
        "isError": False,
        "futureExtension": {"value": "preserve"},
    }
    result = mt.CallToolResult.model_validate(raw)
    replies = ToolReplies()

    received = replies.received(raw, result)

    assert received.raw is raw
    assert received.raw["futureExtension"] == {"value": "preserve"}
    assert received.result is result
    assert replies.text(received, "count", "call-1") == "пять"
    made = replies.made("пять", {"count": 5}, False)
    assert made.raw == {
        "resultType": "complete",
        "content": [{"type": "text", "text": "пять"}],
        "structuredContent": {"count": 5},
        "isError": False,
    }


def test_empty_error_has_call_context_and_success_stays_empty() -> None:
    replies = ToolReplies()
    failed = mt.CallToolResult(content=[], is_error=True)
    succeeded = mt.CallToolResult(content=[])

    assert replies.result_text(failed, "count", "call-1") == (
        "The MCP server reported an error for the call 'call-1' of "
        "tool 'count' without any text; check the server log for the cause."
    )
    assert replies.result_text(succeeded, "count", "call-1") == ""


def test_protocol_error_keeps_code_message_and_data() -> None:
    error = ErrorData(code=-32602, message="Invalid arguments", data={"column": "id"})
    replies = ToolReplies()

    reply = replies.error("count", "call-1", error)

    assert reply.result.is_error
    assert replies.text(reply, "count", "call-1") == (
        "The MCP server answered the call 'call-1' of tool 'count' with "
        'error -32602: Invalid arguments\n{\n  "column": "id"\n}'
    )


def test_prompt_keeps_description_order_and_assistant_role() -> None:
    prompt = mt.GetPromptResult(
        description="Report",
        messages=[
            mt.PromptMessage(role="user", content=mt.TextContent(text="Count rows")),
            mt.PromptMessage(role="assistant", content=mt.TextContent(text="Five")),
        ],
    )

    assert ToolReplies().prompt(prompt) == "Report\n\nCount rows\n\n[assistant] Five"
