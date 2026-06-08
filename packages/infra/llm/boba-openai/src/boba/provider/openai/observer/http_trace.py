"""HTTP-trace дамп LLM-вызовов: сырой request + response + chunks как есть.

В отличие от curl_trace.py, никакой агрегации/реконструкции содержимого:
HTTP-запрос пишется как есть, HTTP-ответ (заголовки) — как есть, дальше
каждый chunk OpenAI-стрима записывается в файл одной строкой JSON
(model_dump_json) без форматирования и без интерпретации.

Назначение — чистый trace, по которому можно полностью восстановить
картину взаимодействия с провайдером: что ушло, что пришло, в каком
порядке. Для отладки протокольных нюансов (порядок deltas, неожиданные
поля от провайдера, поведение reasoning_content и т.п.).

ВНИМАНИЕ: пишет полные заголовки запроса/ответа БЕЗ маскировки —
включая Authorization: Bearer <token>. Файл рассчитан на локальное
расследование, не для шеринга.
"""

from __future__ import annotations

import json
import shlex
import traceback
from collections.abc import Mapping
from typing import Any

import httpx

import openai
from boba.llm.observer import LLMRequestObserver
from boba.workspace.contract import WorkspaceShell
from openai.types.chat import ChatCompletion
from openai.types.chat.chat_completion_chunk import ChatCompletionChunk


class HttpTraceChatCompletionObserver(
    LLMRequestObserver[
        dict[str, Any],
        ChatCompletionChunk,
        ChatCompletion,
        openai.APIError,
        httpx.HTTPError,
    ]
):
    """Сырой trace HTTP-обмена + последовательность chunks в markdown."""

    def __init__(
        self,
        workspace: WorkspaceShell,
        path: str = "http_trace.md",
    ) -> None:
        self._workspace = workspace
        self._path = path

    def on_request(self, request: dict[str, Any]) -> None:
        del request

    def on_http_request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> None:
        cmd = self._format_curl(method, url, headers, body)
        self._append(f"## Request\n\n```bash\n{cmd}```\n\n")

    def on_http_response(
        self,
        status_code: int,
        headers: Mapping[str, str],
    ) -> None:
        lines = [f"HTTP/1.1 {status_code}"]
        for k, v in headers.items():
            lines.append(f"{k}: {v}")

        head = "\n".join(lines)
        self._append(f"## Response\n\n```\n{head}\n```\n\n")
        self._append("## Chunks\n\n```jsonl\n")

    def on_response_chunk(self, chunk: ChatCompletionChunk) -> None:
        try:
            line = chunk.model_dump_json()
        except (TypeError, ValueError):
            line = repr(chunk)
        self._append(line + "\n")

    def on_response(self, response: ChatCompletion) -> None:
        # stream=False: один готовый ответ — пишем целиком одной строкой JSON.
        try:
            line = response.model_dump_json()
        except (TypeError, ValueError):
            line = repr(response)
        self._append(line + "\n")

    def on_request_end(self) -> None:
        self._append("```\n\n")
        self._append("## end: ok\n\n---\n\n")

    def on_request_cancel(self) -> None:
        self._append("```\n\n")
        self._append("## end: cancelled\n\n---\n\n")

    def on_api_exception(self, exception: openai.APIError) -> None:
        self._append("```\n\n")
        self._append("## Error\n\n")
        self._append(f"**{type(exception).__name__}:** {exception}\n\n")
        status_code = getattr(exception, "status_code", None)
        if isinstance(status_code, int):
            self._append(f"_status_code:_ {status_code}\n\n")
        body = getattr(exception, "body", None)
        if body is not None:
            try:
                body_text = json.dumps(body, ensure_ascii=False, indent=2)
            except (TypeError, ValueError):
                body_text = repr(body)
            self._append(f"_body:_\n\n```\n{body_text}\n```\n\n")
        tb = "".join(
            traceback.format_exception(
                type(exception), exception, exception.__traceback__
            )
        )
        self._append(f"```\n{tb}```\n\n")
        self._append(f"## end: raised:{type(exception).__name__}\n\n---\n\n")

    def on_http_exception(self, exception: httpx.HTTPError) -> None:
        self._append("```\n\n")
        self._append("## Error\n\n")
        self._append(f"**{type(exception).__name__}:** {exception}\n\n")
        response = getattr(exception, "response", None)
        if isinstance(response, httpx.Response):
            self._append(f"_status_code:_ {response.status_code}\n\n")
            text = response.text
            if text:
                self._append(f"_body:_\n\n```\n{text}\n```\n\n")
        tb = "".join(
            traceback.format_exception(
                type(exception), exception, exception.__traceback__
            )
        )
        self._append(f"```\n{tb}```\n\n")

        self._append(f"## end: raised:{type(exception).__name__}\n\n---\n\n")

    def _append(self, text: str) -> None:
        with self._workspace.append_text(self._path) as f:
            f.write(text)

    @staticmethod
    def _format_curl(
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> str:
        """curl-команда с heredoc-телом — копипастится в bash без экранирования."""
        lines = [f"curl -X {method} {shlex.quote(url)}"]
        for k, v in headers.items():
            lines.append(f"  -H {shlex.quote(f'{k}: {v}')}")
        pretty = HttpTraceChatCompletionObserver._pretty_json(body)
        if pretty is None:
            return " \\\n".join(lines) + "\n"
        lines.append("  --data-binary @- <<'JSON'")
        return " \\\n".join(lines) + "\n" + pretty + "\nJSON\n"

    @staticmethod
    def _pretty_json(body: bytes) -> str | None:
        if not body:
            return None
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return None
        try:
            return json.dumps(json.loads(text), ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            return text


__all__ = ["HttpTraceChatCompletionObserver"]
