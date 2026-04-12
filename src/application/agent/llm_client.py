"""Парсинг streaming-ответа LLM.

Три StreamTransformer[ChoiceDelta, DocPipelineEvent]:
    ToolCallAccumulator — накопление tool_calls
    ReasoningExtractor  — извлечение reasoning_content
    ContentParser       — парсинг content (<think> теги)

LLMStreamConsumer прогоняет каждый delta через все три (fan-out).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Sequence

from openai import Stream
from openai.types.chat import ChatCompletionChunk
from openai.types.chat.chat_completion_chunk import ChoiceDelta

from domain.agent.events import AnswerToken, DocPipelineEvent, ThinkingToken
from domain.agent.think_parser import ThinkTagParser
from domain.core.streaming import StreamTransformer


# ---------------------------------------------------------------------------
# ToolCallData
# ---------------------------------------------------------------------------

@dataclass
class ToolCallData:
    """Один tool call от LLM."""
    id: str = ""
    name: str = ""
    arguments: str = ""

    def parse_arguments(self) -> Dict[str, Any]:
        if not self.arguments:
            return {}
        try:
            return json.loads(self.arguments)
        except json.JSONDecodeError:
            return {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


# ---------------------------------------------------------------------------
# StreamTransformer[ChoiceDelta, DocPipelineEvent] — три реализации
# ---------------------------------------------------------------------------

class ToolCallAccumulator(StreamTransformer[ChoiceDelta, DocPipelineEvent]):
    """Накапливает tool_calls из delta. Не yield'ит events.

    Результат — в .tool_calls после завершения потока.
    """

    def __init__(self) -> None:
        self._pending: Dict[int, ToolCallData] = {}

    def feed(self, delta: ChoiceDelta) -> Iterator[DocPipelineEvent]:
        if not delta.tool_calls:
            return
        for tc_delta in delta.tool_calls:
            idx = tc_delta.index
            if idx not in self._pending:
                self._pending[idx] = ToolCallData()
            tc = self._pending[idx]
            if tc_delta.id:
                tc.id = tc_delta.id
            if tc_delta.function:
                if tc_delta.function.name:
                    tc.name = tc_delta.function.name
                if tc_delta.function.arguments:
                    tc.arguments += tc_delta.function.arguments
        yield from ()

    @property
    def tool_calls(self) -> List[ToolCallData]:
        return [self._pending[i] for i in sorted(self._pending)]

    @property
    def has_tool_calls(self) -> bool:
        return len(self._pending) > 0

    def reset(self) -> None:
        self._pending.clear()


class ReasoningExtractor(StreamTransformer[ChoiceDelta, DocPipelineEvent]):
    """Извлекает reasoning_content из delta → ThinkingToken.

    Для моделей с отдельным полем reasoning (DeepSeek, QwQ).
    """

    def feed(self, delta: ChoiceDelta) -> Iterator[DocPipelineEvent]:
        reasoning = getattr(delta, "reasoning_content", None) or ""
        if reasoning:
            yield ThinkingToken(token=reasoning)

    def reset(self) -> None:
        pass


class ContentParser(StreamTransformer[ChoiceDelta, DocPipelineEvent]):
    """Парсит content из delta через ThinkTagParser → ThinkingToken / AnswerToken."""

    def __init__(self) -> None:
        self._parser = ThinkTagParser()
        self._answer_tokens: List[str] = []

    def feed(self, delta: ChoiceDelta) -> Iterator[DocPipelineEvent]:
        content = delta.content or ""
        if not content:
            return
        for fragment in self._parser.feed(content):
            if not fragment.text:
                continue
            if fragment.role.value == "thinking":
                yield ThinkingToken(token=fragment.text)
            else:
                self._answer_tokens.append(fragment.text)
                yield AnswerToken(token=fragment.text)

    @property
    def answer(self) -> str:
        return "".join(self._answer_tokens)

    def reset(self) -> None:
        self._parser.reset()
        self._answer_tokens.clear()


# Тип-алиас для всех delta handlers
DeltaHandler = StreamTransformer[ChoiceDelta, DocPipelineEvent]


# ---------------------------------------------------------------------------
# LLMStreamConsumer — fan-out через StreamTransformers
# ---------------------------------------------------------------------------

@dataclass
class LLMStreamConsumer:
    """StreamConsumer: прогоняет каждый delta через набор handlers."""
    tool_calls: List[ToolCallData] = field(default_factory=list)
    answer: str = ""

    def consume(self, stream: Stream[ChatCompletionChunk]) -> Iterator[DocPipelineEvent]:
        tc = ToolCallAccumulator()
        reasoning = ReasoningExtractor()
        content = ContentParser()
        handlers: Sequence[DeltaHandler] = [tc, reasoning, content]

        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            for handler in handlers:
                yield from handler.feed(delta)

        self.tool_calls = tc.tool_calls
        self.answer = content.answer

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0
