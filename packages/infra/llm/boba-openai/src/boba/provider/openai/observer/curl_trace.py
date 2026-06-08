"""
Curl-trace дамп LLM-вызовов: copy-paste-ready bash-команда + сырой ответ.

ВНИМАНИЕ: пишет полные заголовки запроса/ответа БЕЗ маскировки —
включая Authorization: Bearer <token>. Файл curl_trace.md
рассчитан на ручное воспроизведение команды; держать его за пределами
доверенного локального окружения нельзя.
"""

from __future__ import annotations

import json
import shlex
import traceback
from collections.abc import Iterable, Mapping
from typing import Any

import httpx

import openai
from boba.llm.observer import LLMRequestObserver
from boba.workspace.contract import WorkspaceShell
from openai.types.chat import ChatCompletion
from openai.types.chat.chat_completion_chunk import ChatCompletionChunk


class CurlTraceChatCompletionObserver(
    LLMRequestObserver[
        dict[str, Any],
        ChatCompletionChunk,
        ChatCompletion,
        openai.APIError,
        httpx.HTTPError,
    ]
):
    """Пишет curl-команду + статус/заголовки/чанки ответа в markdown-файл."""

    def __init__(
        self,
        workspace: WorkspaceShell,
        path: str = "curl_trace.md",
    ) -> None:
        self._workspace = workspace
        self._path = path
        self._reset_state()

    def _reset_state(self) -> None:
        self._tool_calls: dict[int, dict[str, Any]] = {}
        self._finish_reason: str | None = None
        self._usage: tuple[int, int, int] | None = None
        self._current_section: str | None = None

    def on_request(self, request: dict[str, Any]) -> None:
        del request
        self._reset_state()

    def on_http_request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> None:
        cmd = self._format_curl(method, url, headers, body)
        self._append(f"## curl\n\n```bash\n{cmd}```\n\n")

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

    def on_response_chunk(self, chunk: ChatCompletionChunk) -> None:
        for choice in chunk.choices:
            delta = choice.delta
            r = (delta.model_extra or {}).get("reasoning_content")
            if r:
                self._emit_section("Thinking", str(r))
            if delta.content:
                self._emit_section("Answer", delta.content)
            if delta.refusal:
                self._emit_section("Refusal", delta.refusal)
            if delta.tool_calls:
                self._absorb_tool_calls(delta.tool_calls)
            if choice.finish_reason and self._finish_reason is None:
                self._finish_reason = choice.finish_reason

        if chunk.usage is not None and self._usage is None:
            u = chunk.usage
            self._usage = (u.prompt_tokens, u.completion_tokens, u.total_tokens)

    def on_response(self, response: ChatCompletion) -> None:
        # stream=False: реконструируем секции из готового message (без дельт).
        for choice in response.choices:
            message = choice.message
            r = (message.model_extra or {}).get("reasoning_content")
            if r:
                self._emit_section("Thinking", str(r))
            if message.content:
                self._emit_section("Answer", message.content)
            if message.refusal:
                self._emit_section("Refusal", message.refusal)
            if message.tool_calls:
                self._absorb_message_tool_calls(message.tool_calls)
            if choice.finish_reason and self._finish_reason is None:
                self._finish_reason = choice.finish_reason

        if response.usage is not None and self._usage is None:
            u = response.usage
            self._usage = (u.prompt_tokens, u.completion_tokens, u.total_tokens)

    def on_request_end(self) -> None:
        self._close_open_section()
        self._dump_tool_calls()
        self._dump_usage()
        self._append("## end: ok\n\n---\n\n")

    def on_request_cancel(self) -> None:
        self._close_open_section()
        self._dump_tool_calls()
        self._dump_usage()
        self._append("## end: cancelled\n\n---\n\n")

    def on_api_exception(self, exception: openai.APIError) -> None:
        self._close_open_section()
        self._dump_tool_calls()
        self._dump_usage()
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
        self._close_open_section()
        self._dump_tool_calls()
        self._dump_usage()
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

    def _close_open_section(self) -> None:
        if self._current_section is not None:
            self._append("\n\n")
            self._current_section = None

    def _dump_tool_calls(self) -> None:
        for idx in sorted(self._tool_calls):
            tc = self._tool_calls[idx]
            name = tc["name"] or "(none)"
            tc_id = tc["id"] or "(none)"
            args_text = "".join(tc["args"])
            self._append(f"## Tool call #{idx}: {name} (id={tc_id})\n\n")
            self._append(f"```json\n{args_text}\n```\n\n")

    def _dump_usage(self) -> None:
        if self._usage is None:
            return
        p, c, t = self._usage
        self._append(f"_usage prompt={p} completion={c} total={t}_\n\n")

    def _emit_section(self, title: str, text: str) -> None:
        if self._current_section != title:
            if self._current_section is not None:
                self._append("\n\n")
            self._append(f"## {title}\n\n")
            self._current_section = title
        self._append(text)

    def _absorb_tool_calls(self, tool_calls: Iterable[Any]) -> None:
        for tc in tool_calls:
            entry = self._tool_calls.setdefault(
                tc.index,
                {"name": None, "id": None, "args": []},
            )
            if tc.id:
                entry["id"] = tc.id
            if tc.function:
                if tc.function.name:
                    entry["name"] = tc.function.name
                if tc.function.arguments:
                    entry["args"].append(tc.function.arguments)

    def _absorb_message_tool_calls(self, tool_calls: Iterable[Any]) -> None:
        # tool_calls готового message не несут index — берём порядковый.
        for index, tc in enumerate(tool_calls):
            entry = self._tool_calls.setdefault(
                index,
                {"name": None, "id": None, "args": []},
            )
            if tc.id:
                entry["id"] = tc.id
            function = getattr(tc, "function", None)
            if function is not None:
                if function.name:
                    entry["name"] = function.name
                if function.arguments:
                    entry["args"].append(function.arguments)

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
        pretty = CurlTraceChatCompletionObserver._pretty_json(body)
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
