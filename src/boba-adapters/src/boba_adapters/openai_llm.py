"""OpenAI-реализация LLMCompletionService.

Оборачивает openai.OpenAI SDK, конвертирует ChoiceDelta → CompletionDelta.
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, Sequence

from openai import OpenAI

from boba_adapters.openai_adapter import to_openai_messages, to_openai_tools
from boba_domain.chat.messages import LLMMessage
from boba_domain.core.llm_service import CompletionDelta


class OpenAICompletionService:
    """LLMCompletionService через OpenAI-совместимый API."""

    def __init__(self, client: OpenAI) -> None:
        self._client = client

    def stream_completion(
        self,
        messages: Sequence[LLMMessage],
        tool_definitions: Sequence[Dict[str, Any]],
        model: str,
    ) -> Iterator[CompletionDelta]:
        api_messages = list(to_openai_messages(iter(messages)))
        api_tools = list(to_openai_tools(iter(tool_definitions)))

        stream = self._client.chat.completions.create(
            model=model,
            messages=api_messages,
            tools=api_tools,
            stream=True,
        )

        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            if delta.tool_calls:
                for tc in delta.tool_calls:
                    func = tc.function
                    yield CompletionDelta(
                        tool_call_index=tc.index,
                        tool_call_id=tc.id or None,
                        tool_call_name=func.name if func else None,
                        tool_call_arguments=func.arguments if func else None,
                    )

            if delta.content:
                yield CompletionDelta(content=delta.content)

            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                yield CompletionDelta(reasoning_content=reasoning)
