"""OpenAIChatVisitor: ToolResult → str для Chat Completions tool-message."""

from __future__ import annotations

import json

from boba.tools.domain import ErrorResult, JsonResult, TextResult, ToolResultVisitor

__all__ = ["OpenAIChatVisitor"]


class OpenAIChatVisitor(ToolResultVisitor[str]):
    """ToolResult → строка для tool-message Chat Completions API.

    Когда переключимся на Responses API — появится отдельный
    `OpenAIResponsesVisitor[list[Part]]` (или `[dict]`), а этот
    останется для Chat-режима.
    """

    def visit_text(self, result: TextResult) -> str:
        return result.text

    def visit_json(self, result: JsonResult) -> str:
        return json.dumps(result.payload, ensure_ascii=False)

    def visit_error(self, result: ErrorResult) -> str:
        return result.message
