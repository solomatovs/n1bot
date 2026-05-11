"""Curl-trace дамп LLM-вызовов: copy-paste-ready bash-команда + сырой ответ.

ВНИМАНИЕ: пишет полные заголовки запроса/ответа БЕЗ маскировки —
включая `Authorization: Bearer <token>`. Файл curl_trace.md
рассчитан на ручное воспроизведение команды; держать его за пределами
доверенного локального окружения нельзя.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Iterable, Mapping
from typing import Any, ClassVar

from boba.llm.observer import LLMRequestObserver, RequestOutcome
from boba.workspace.contract import WorkspaceShell
from openai.types.chat.chat_completion_chunk import ChatCompletionChunk


class CurlTraceChatCompletionObserver(
    LLMRequestObserver[dict[str, Any], ChatCompletionChunk]
):
    """Пишет curl-команду + статус/заголовки/чанки ответа в markdown-файл."""

    _REASONING_KEY: ClassVar[str] = "reasoning_content"

    def __init__(
        self,
        workspace: WorkspaceShell,
        response_chunks: bool = True,
        path: str = "curl_trace.md",
    ) -> None:
        self._workspace = workspace
        self._path = path
        self._response_chunk = response_chunks
        self._reset_state()

    def _reset_state(self) -> None:
        self._reasoning: list[str] = []
        self._answer: list[str] = []
        self._refusal: list[str] = []
        self._tool_calls: dict[int, dict[str, Any]] = {}
        self._finish_reason: str | None = None
        self._usage: tuple[int, int, int] | None = None

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
        self._append(f"## Response\n\n```\n{head}\n```\n\n```jsonl\n")

    def on_response_chunk(self, chunk: ChatCompletionChunk) -> None:
        if self._response_chunk:
            self._append(chunk.model_dump_json() + "\n")

        for choice in chunk.choices:
            delta = choice.delta
            r = (delta.model_extra or {}).get(self._REASONING_KEY)
            if r:
                self._reasoning.append(str(r))
            if delta.content:
                self._answer.append(delta.content)
            if delta.refusal:
                self._refusal.append(delta.refusal)
            if delta.tool_calls:
                self._absorb_tool_calls(delta.tool_calls)
            if choice.finish_reason and self._finish_reason is None:
                self._finish_reason = choice.finish_reason

        if chunk.usage is not None and self._usage is None:
            u = chunk.usage
            self._usage = (u.prompt_tokens, u.completion_tokens, u.total_tokens)

    def on_request_end(self, outcome: RequestOutcome) -> None:
        self._append("```\n\n")
        self._append(self._render_aggregated())
        self._append(f"## end: {outcome.label()}\n\n---\n\n")

    def _render_aggregated(self) -> str:
        parts: list[str] = ["## Aggregated\n\n"]
        if self._reasoning:
            parts.append("### Reasoning\n\n")
            parts.append("".join(self._reasoning).rstrip() + "\n\n")
        if self._answer:
            parts.append("### Answer\n\n")
            parts.append("".join(self._answer).rstrip() + "\n\n")
        if self._refusal:
            parts.append("### Refusal\n\n")
            parts.append("".join(self._refusal).rstrip() + "\n\n")
        for idx in sorted(self._tool_calls):
            tc = self._tool_calls[idx]
            name = tc["name"] or "(none)"
            tc_id = tc["id"] or "(none)"
            args_text = "".join(tc["args"])
            parts.append(f"### Tool call #{idx}: {name} (id={tc_id})\n\n")
            parts.append(f"```json\n{args_text}\n```\n\n")
        meta: list[str] = []
        if self._finish_reason:
            pass
        if self._usage is not None:
            p, c, t = self._usage
            meta.append(f"usage prompt={p} completion={c} total={t}")
        if meta:
            parts.append("_" + " | ".join(meta) + "_\n\n")
        return "".join(parts)

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
