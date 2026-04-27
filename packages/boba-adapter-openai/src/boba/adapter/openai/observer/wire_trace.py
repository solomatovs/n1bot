"""Wire-trace дамп OpenAI Chat Completions: kwargs запроса и каждый
chunk ответа в виде JSON-секций markdown-файла.

Назначение — отладка SDK/прокси и сбор датасетов: видно ровно то, что
ушло провайдеру и что приходит обратно по проводу, до любой доменной
конверсии.
"""

from __future__ import annotations

import json
from typing import Any

from boba.domain.core.workspace import HistoryWorkspaceShell
from boba.domain.llm.observer import LLMRequestObserver, RequestOutcome
from openai.types.chat.chat_completion_chunk import ChatCompletionChunk


class WireTraceChatCompletionObserver(
    LLMRequestObserver[dict[str, Any], ChatCompletionChunk]
):
    """Пишет сырые kwargs запроса и каждый chunk ответа в markdown-файл
    внутри workspace.

    Каждый вызов — отдельная секция с заголовком (## Request /
    ## Response chunk) и блоком json. Файл открывается на каждый
    вызов в режиме append — состояние на уровне файловой системы,
    между перезапусками агента накопление продолжается.
    """

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
