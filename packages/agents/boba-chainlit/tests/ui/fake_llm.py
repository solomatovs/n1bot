"""Фейковый OpenAI-совместимый провайдер: отдаёт SSE по токену с задержкой.

Нужен интеграционным тестам ленты: сценарий выбирается по тексту последнего
сообщения пользователя, поэтому тест диктует, какие шаги нарисует ход, и знает
тайминг каждого токена.

Ошибки: ScenarioError — в запросе нет сценария с таким именем.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

__all__ = [
    "FakeLlmApp",
    "Scenario",
    "ScenarioError",
    "ScenarioName",
    "ToolCallSpec",
    "TurnScript",
]


class ScenarioError(Exception):
    """Запрошен сценарий, которого нет."""


class ScenarioName(StrEnum):
    """Сценарии, которые умеет разыгрывать фейковый провайдер."""

    THINKING = "scenario:thinking"
    ANSWER = "scenario:answer"
    THINKING_ANSWER = "scenario:thinking-answer"
    TOOL = "scenario:tool"
    TOOL_ERROR = "scenario:tool-error"
    DIAGRAM = "scenario:diagram"

    @classmethod
    def of(cls, text: str) -> ScenarioName:
        """Ищет маркер сценария в сообщении пользователя.

        Маркеры вложены друг в друга ('scenario:tool' — префикс
        'scenario:tool-error'), поэтому побеждает самый длинный.
        """
        ordered = sorted(cls, key=lambda name: len(name.value), reverse=True)
        for name in ordered:
            if name.value in text:
                return name

        raise ScenarioError(f"no scenario in message: {text[:80]!r}")


@dataclass
class ToolCallSpec:
    """Вызов инструмента, который провайдер попросит выполнить."""

    call_id: str
    name: str
    arguments: str


@dataclass
class TurnScript:
    """Один ответ провайдера: рассуждения, текст и вызовы инструментов."""

    reasoning: str = ""
    content: str = ""
    tool_calls: Sequence[ToolCallSpec] = ()

    def finish_reason(self) -> str:
        if self.tool_calls:
            return "tool_calls"

        return "stop"


@dataclass
class Scenario:
    """Последовательность ответов провайдера на один и тот же тред."""

    turns: Sequence[TurnScript]

    def turn(self, index: int) -> TurnScript:
        if index < len(self.turns):
            return self.turns[index]

        return self.turns[-1]


class ScenarioBook:
    """Готовые сценарии: по одному на каждый тип шага ленты."""

    DIAGRAM_SPEC: str = "erDiagram\\n    USER ||--o{ ORDER : places"

    @classmethod
    def of(cls, name: ScenarioName) -> Scenario:
        builders = {
            ScenarioName.THINKING: cls._thinking,
            ScenarioName.ANSWER: cls._answer,
            ScenarioName.THINKING_ANSWER: cls._thinking_answer,
            ScenarioName.TOOL: cls._tool,
            ScenarioName.TOOL_ERROR: cls._tool_error,
            ScenarioName.DIAGRAM: cls._diagram,
        }
        build = builders.get(name)
        if build is None:
            raise ScenarioError(f"scenario is not scripted: {name}")

        return build()

    @staticmethod
    def _thinking() -> Scenario:
        return Scenario(
            turns=[TurnScript(reasoning="I am thinking about it slowly", content="ok")]
        )

    @staticmethod
    def _answer() -> Scenario:
        return Scenario(turns=[TurnScript(content="Here is a plain streamed answer")])

    @staticmethod
    def _thinking_answer() -> Scenario:
        return Scenario(
            turns=[
                TurnScript(
                    reasoning="First I reason about the question",
                    content="Then I answer the question",
                )
            ]
        )

    @classmethod
    def _tool(cls) -> Scenario:
        call = ToolCallSpec(
            call_id="call_stream_logs",
            name="stream_logs_usage",
            arguments="{}",
        )
        return Scenario(
            turns=[
                TurnScript(reasoning="I need the journal usage", tool_calls=[call]),
                TurnScript(content="The journal usage is above"),
            ]
        )

    @classmethod
    def _tool_error(cls) -> Scenario:
        call = ToolCallSpec(
            call_id="call_broken",
            name="stream_logs_cleanup",
            arguments=json.dumps({"thread_id": "no-such-thread"}),
        )
        return Scenario(
            turns=[
                TurnScript(
                    reasoning="I will purge a missing thread", tool_calls=[call]
                ),
                TurnScript(content="The purge failed"),
            ]
        )

    @classmethod
    def _diagram(cls) -> Scenario:
        call = ToolCallSpec(
            call_id="call_diagram",
            name="diagram_save",
            arguments=json.dumps(
                {
                    "name": "orders.mmd",
                    "spec": "erDiagram\n    USER ||--o{ ORDER : places",
                }
            ),
        )
        return Scenario(
            turns=[
                TurnScript(reasoning="I will draw the diagram", tool_calls=[call]),
                TurnScript(content="The diagram is drawn"),
            ]
        )


@dataclass
class FakeLlmApp:
    """ASGI-приложение провайдера: счётчик ходов на сценарий и журнал запросов."""

    token_delay_sec: float = 0.02
    model: str = "fake-model"
    turns_done: dict[str, int] = field(default_factory=dict)
    requests: list[dict[str, Any]] = field(default_factory=list)

    def asgi(self) -> FastAPI:
        app = FastAPI()

        @app.get("/health")
        async def health() -> dict[str, str]:
            return {"status": "ok"}

        @app.post("/reset")
        async def reset() -> dict[str, str]:
            """Сброс счётчика ходов и журнала: тест начинает с чистого листа."""
            self.turns_done.clear()
            self.requests.clear()
            return {"status": "ok"}

        @app.get("/requests")
        async def recorded() -> JSONResponse:
            """Журнал полных запросов провайдеру: тест сверяет параметры модели."""
            return JSONResponse({"requests": self.requests})

        @app.post("/v1/chat/completions")
        async def completions(request: Request) -> Response:
            payload = await request.json()
            self.requests.append(payload)
            name = ScenarioName.of(self._last_user_text(payload))
            index = self.turns_done.get(name.value, 0)
            self.turns_done[name.value] = index + 1
            script = ScenarioBook.of(name).turn(index)

            if not payload.get("stream"):
                return JSONResponse(self._completion(script))

            return StreamingResponse(
                self._stream(script),
                media_type="text/event-stream",
            )

        return app

    @staticmethod
    def _last_user_text(payload: dict[str, Any]) -> str:
        messages = payload.get("messages")
        if not messages:
            raise ScenarioError("request has no messages")

        for message in reversed(messages):
            if message.get("role") != "user":
                continue

            content = message.get("content")
            if isinstance(content, str):
                return content

        raise ScenarioError("request has no user message")

    def _completion(self, script: TurnScript) -> dict[str, Any]:
        """Ответ без стрима: текст, рассуждения и вызовы приходят разом."""
        message: dict[str, Any] = {"role": "assistant", "content": script.content}
        if script.reasoning:
            message["reasoning"] = script.reasoning

        calls: list[dict[str, Any]] = []
        for index, call in enumerate(script.tool_calls):
            calls.append(
                {
                    "index": index,
                    "id": call.call_id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
            )
        if calls:
            message["tool_calls"] = calls

        return {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "created": 1,
            "model": self.model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": script.finish_reason(),
                }
            ],
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
            },
        }

    async def _stream(self, script: TurnScript) -> AsyncIterator[bytes]:
        for chunk in self._chunks(script):
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
            await asyncio.sleep(self.token_delay_sec)

        yield b"data: [DONE]\n\n"

    def _chunks(self, script: TurnScript) -> Iterator[dict[str, Any]]:
        for token in self._tokens(script.reasoning):
            yield self._delta({"role": "assistant", "content": "", "reasoning": token})

        for token in self._tokens(script.content):
            yield self._delta({"role": "assistant", "content": token})

        for index, call in enumerate(script.tool_calls):
            delta = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "index": index,
                        "id": call.call_id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": call.arguments},
                    }
                ],
            }
            yield self._delta(delta)

        yield self._delta({"role": "assistant", "content": ""}, script.finish_reason())

    @staticmethod
    def _tokens(text: str) -> Iterator[str]:
        if not text:
            return

        for index, word in enumerate(text.split(" ")):
            if index:
                yield f" {word}"
                continue

            yield word

    def _delta(
        self, delta: dict[str, Any], finish_reason: str | None = None
    ) -> dict[str, Any]:
        return {
            "id": "chatcmpl-fake",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": self.model,
            "choices": [
                {"index": 0, "delta": delta, "finish_reason": finish_reason},
            ],
        }


def serve(host: str, port: int, token_delay_sec: float) -> None:
    """Запуск провайдера отдельным процессом."""
    app = FakeLlmApp(token_delay_sec=token_delay_sec)
    uvicorn.run(app.asgi(), host=host, port=port, log_level="warning")
