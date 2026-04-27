"""Markdown-стенограмма OpenAI Chat Completions запроса/ответа."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from boba.domain.core.workspace import HistoryWorkspaceShell
from boba.domain.llm.observer import LLMRequestObserver, RequestOutcome
from openai.types.chat.chat_completion_chunk import ChatCompletionChunk


class TranscriptChatCompletionObserver(
    LLMRequestObserver[dict[str, Any], ChatCompletionChunk]
):
    """Пишет markdown-лог запроса/ответа OpenAI Chat Completions."""

    _REASONING_KEY = "reasoning_content"

    def __init__(
        self,
        workspace: HistoryWorkspaceShell,
        path: str = "transcript.md",
    ) -> None:
        self._workspace = workspace
        self._path = path
        self._reset_state()

    def _reset_state(self) -> None:
        self._request_kwargs: dict[str, Any] = {}
        self._reasoning: list[str] = []
        self._answer: list[str] = []
        self._refusal: list[str] = []
        self._tool_calls: dict[int, dict[str, Any]] = {}
        self._finish_reason: str | None = None
        self._usage: tuple[int, int, int] | None = None

    def on_request(self, request: dict[str, Any]) -> None:
        self._reset_state()
        self._request_kwargs = request

    def on_response_chunk(self, chunk: ChatCompletionChunk) -> None:
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

    def on_request_end(self, outcome: RequestOutcome) -> None:
        body = json.dumps(
            self._request_kwargs, ensure_ascii=False, indent=2, default=str
        )
        parts: list[str] = [f"\n\n## Request\n\n`json\n{body}\n`\n\n## Response\n"]

        if self._reasoning:
            parts.append("\n### Reasoning\n\n")
            parts.append("".join(self._reasoning))
            parts.append("\n")

        if self._answer:
            parts.append("\n### Answer\n\n")
            parts.append("".join(self._answer))
            parts.append("\n")

        if self._refusal:
            parts.append("\n### Refusal\n\n")
            parts.append("".join(self._refusal))
            parts.append("\n")

        for idx in sorted(self._tool_calls):
            tc = self._tool_calls[idx]
            name = tc["name"] or "(none)"
            tc_id = tc["id"] or "(none)"
            args_text = "".join(tc["args"])
            parts.append(f"\n### Tool call #{idx}: {name} (id={tc_id})\n\n")
            parts.append(f"`\n{args_text}\n`\n")

        if self._finish_reason:
            parts.append(f"\n_finish_reason={self._finish_reason}_\n")

        if self._usage is not None:
            prompt, completion, total = self._usage
            parts.append(
                f"_usage: prompt={prompt} completion={completion} total={total}_\n"
            )

        parts.append(f"\n## End: {outcome.label()}\n")

        with self._workspace.append_text(self._path) as f:
            f.write("".join(parts))
