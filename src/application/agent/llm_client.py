"""Парсинг streaming-ответа LLM."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List

from domain.agent.events import AnswerToken, DocPipelineEvent, ThinkingToken
from domain.agent.think_parser import ThinkTagParser


@dataclass
class ToolCallData:
    """Один tool call от LLM. Аккумулятор при стриминге."""
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


@dataclass
class LLMStreamConsumer:
    """Потребляет streaming-ответ LLM, yield'ит токены, накапливает результат."""
    tool_calls: List[ToolCallData] = field(default_factory=list)
    answer: str = ""

    def consume(self, stream: Any) -> Iterator[DocPipelineEvent]:
        """Потребить stream, yield'ить токены по мере поступления."""
        pending: Dict[int, ToolCallData] = {}
        answer_tokens: List[str] = []
        parser = ThinkTagParser()

        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in pending:
                        pending[idx] = ToolCallData()
                    tc = pending[idx]
                    if tc_delta.id:
                        tc.id = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            tc.name = tc_delta.function.name
                        if tc_delta.function.arguments:
                            tc.arguments += tc_delta.function.arguments

            reasoning = getattr(delta, "reasoning_content", None) or ""
            if reasoning:
                yield ThinkingToken(token=reasoning)

            content = getattr(delta, "content", None) or ""
            if content:
                for fragment in parser.feed(content):
                    if not fragment.text:
                        continue
                    if fragment.role.value == "thinking":
                        yield ThinkingToken(token=fragment.text)
                    else:
                        answer_tokens.append(fragment.text)
                        yield AnswerToken(token=fragment.text)

        self.tool_calls = [pending[i] for i in sorted(pending)]
        self.answer = "".join(answer_tokens)

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0
