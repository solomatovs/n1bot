"""Wire-trace дамп OpenAI Chat Completions в markdown-файл."""

from __future__ import annotations

import json
from typing import Any

from boba.domain.core.workspace import HistoryWorkspaceShell
from boba.domain.llm.observer import LLMRequestObserver, RequestOutcome
from openai.types.chat.chat_completion_chunk import ChatCompletionChunk


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

    def on_response_chunk(self, chunk: ChatCompletionChunk) -> None:
        body = chunk.model_dump_json(indent=2)
        self._append(f"## Response chunk\n\n`json\n{body}\n`\n\n")

    def on_request_end(self, outcome: RequestOutcome) -> None:
        self._append(f"## End: {outcome.label()}\n\n")

    def _append(self, text: str) -> None:
        with self._workspace.append_text(self._path) as f:
            f.write(text)
