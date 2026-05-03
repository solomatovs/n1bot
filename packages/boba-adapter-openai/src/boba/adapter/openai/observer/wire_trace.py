"""Wire-trace дамп OpenAI Chat Completions в markdown-файл."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from boba_next.llm.observer import LLMRequestObserver, RequestOutcome
from boba_next.workspace import HistoryWorkspaceShell

from openai.types.chat.chat_completion_chunk import ChatCompletionChunk

_SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "api-key",
        "x-api-key",
        "openai-api-key",
        "cookie",
        "set-cookie",
    }
)


def _mask_value(name: str, value: str) -> str:
    if name.lower() != "authorization":
        return "***"
    scheme, _, _ = value.partition(" ")
    return f"{scheme} ***" if scheme else "***"


def _mask_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        k: _mask_value(k, v) if k.lower() in _SENSITIVE_HEADERS else v
        for k, v in headers.items()
    }


def _format_body(body: bytes) -> str:
    if not body:
        return "(empty)"
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return f"(binary, {len(body)} bytes)"
    try:
        return json.dumps(json.loads(text), ensure_ascii=False, indent=2)
    except json.JSONDecodeError:
        return text


class WireTraceChatCompletionObserver(
    LLMRequestObserver[dict[str, Any], ChatCompletionChunk]
):
    """Пишет сырые kwargs запроса и chunk-и ответа в markdown-файл."""

    def __init__(
        self,
        workspace: HistoryWorkspaceShell,
        path: str = "wire_trace.md",
    ) -> None:
        self._workspace = workspace
        self._path = path

    def on_request(self, request: dict[str, Any]) -> None:
        body = json.dumps(request, ensure_ascii=False, indent=2, default=str)
        self._append(f"## Request\n\n`json\n{body}\n`\n\n")

    def on_http_request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> None:
        headers_json = json.dumps(_mask_headers(headers), ensure_ascii=False, indent=2)
        body_str = _format_body(body)
        self._append(
            f"## HTTP Request\n\n"
            f"`{method} {url}`\n\n"
            f"Headers:\n\n`json\n{headers_json}\n`\n\n"
            f"Body:\n\n`json\n{body_str}\n`\n\n"
        )

    def on_http_response(
        self,
        status_code: int,
        headers: Mapping[str, str],
    ) -> None:
        headers_json = json.dumps(_mask_headers(headers), ensure_ascii=False, indent=2)
        self._append(
            f"## HTTP Response {status_code}\n\n"
            f"Headers:\n\n`json\n{headers_json}\n`\n\n"
        )

    def on_response_chunk(self, chunk: ChatCompletionChunk) -> None:
        body = chunk.model_dump_json(indent=2)
        self._append(f"## Response chunk\n\n`json\n{body}\n`\n\n")

    def on_request_end(self, outcome: RequestOutcome) -> None:
        self._append(f"## End: {outcome.label()}\n\n")

    def _append(self, text: str) -> None:
        with self._workspace.append_text(self._path) as f:
            f.write(text)
