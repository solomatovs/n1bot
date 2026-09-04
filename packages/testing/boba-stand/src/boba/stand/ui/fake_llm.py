"""Фейковый OpenAI-совместимый провайдер: отдаёт SSE по токену с задержкой.

Нужен интеграционным тестам ленты: сценарий выбирается по тексту последнего
сообщения пользователя, поэтому тест диктует, какие шаги нарисует ход, и знает
тайминг каждого токена.

Ошибки: ScenarioError — в запросе нет сценария с таким именем.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

__all__ = [
    "FakeLlmApp",
    "FakePage",
    "FakeRoute",
    "Scenario",
    "ScenarioError",
    "ScenarioName",
    "ToolCallSpec",
    "TurnScript",
]


class ScenarioError(Exception):
    """Запрошен сценарий, которого нет."""


class FakeRoute(StrEnum):
    """Маршруты фейкового сервера: провайдер модели и страницы для web-тулов."""

    HEALTH = "/health"
    PAGE = "/page"
    LINES = "/lines"
    RESET = "/reset"
    REQUESTS = "/requests"
    COMPLETIONS = "/v1/chat/completions"


class FakePage(StrEnum):
    """Тела страниц стенда: web-инструменты читают их по whitelist'у."""

    HTML = "<html><body><h1>stand page</h1><p>fake llm serves html</p></body></html>"
    LINES = "stand line one\nstand line two\nstand line three"

    @property
    def media_type(self) -> str:
        if self is FakePage.HTML:
            return "text/html"

        return "text/plain"

    @property
    def route(self) -> FakeRoute:
        if self is FakePage.HTML:
            return FakeRoute.PAGE

        return FakeRoute.LINES


class ScenarioName(StrEnum):
    """Сценарии, которые умеет разыгрывать фейковый провайдер."""

    THINKING = "scenario:thinking"
    ANSWER = "scenario:answer"
    THINKING_ANSWER = "scenario:thinking-answer"
    CALL = "scenario:call"
    """Вызов любого инструмента: аргументы приходят в самом сообщении."""

    TOOL = "scenario:tool"
    TOOL_ERROR = "scenario:tool-error"
    DIAGRAM = "scenario:diagram"
    LONG = "scenario:long"
    """Длинный ход для замеров: рассуждения, вызов инструмента и много токенов."""

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

        markers = [name.value for name in ordered]
        msg = (
            f"fake llm: none of the scenario markers {markers} in message {text[:80]!r}"
        )
        raise ScenarioError(msg)


@dataclass
class ToolCallSpec:
    """Вызов инструмента, который провайдер попросит выполнить.

    Подпись вызова intent обязательна у каждого инструмента приложения, и
    настоящая модель её заполняет; фейк ведёт себя так же — дописывает
    подпись, если сценарий её не задал.
    """

    call_id: str
    name: str
    arguments: str

    INTENT_FIELD: ClassVar[str] = "intent"

    def __post_init__(self) -> None:
        parsed = json.loads(self.arguments)
        if not isinstance(parsed, dict):
            got = type(parsed).__name__
            msg = (
                f"scripted call {self.call_id} of {self.name}: arguments expect a "
                f"JSON object, got {got}: {self.arguments[:200]}"
            )
            raise ScenarioError(msg)

        if self.INTENT_FIELD not in parsed:
            parsed[self.INTENT_FIELD] = f"stand call of {self.name}"

        self.arguments = json.dumps(parsed, ensure_ascii=False)


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

    CALL_ANSWER: str = "the tool has answered"

    LONG_WORDS: ClassVar[tuple[str, ...]] = (
        "the",
        "model",
        "reasons",
        "about",
        "the",
        "request",
        "step",
        "by",
        "step",
        "checking",
        "tables",
        "joins",
        "filters",
        "and",
        "the",
        "expected",
        "shape",
        "of",
        "the",
        "answer",
        "before",
        "calling",
        "any",
        "tool",
    )
    """Словарь длинного хода: текст собирается по кругу, токен — слово."""

    LONG_REASONING_WORDS: ClassVar[int] = 60
    LONG_ANSWER_WORDS: ClassVar[int] = 40

    @classmethod
    def of(cls, name: ScenarioName, text: str = "") -> Scenario:
        if name is ScenarioName.CALL:
            return cls._call(text)

        if name is ScenarioName.LONG:
            return cls._long(text)

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
            scripted: list[str] = []
            for scenario in builders:
                scripted.append(scenario.value)

            msg = (
                f"fake llm: scenario {name.value!r} is not scripted, "
                f"known are {sorted(scripted)}"
            )
            raise ScenarioError(msg)

        return build()

    @classmethod
    def _call(cls, text: str) -> Scenario:
        """Инструмент и аргументы диктует сам тест: `scenario:call {json}`.

        Id вызова несёт хеш сообщения: два вызова одного инструмента в одном
        треде получают разные шаги ленты, а не перезаписывают один.
        """
        _, _, tail = text.partition(ScenarioName.CALL.value)
        try:
            request = json.loads(tail.strip())
        except json.JSONDecodeError as exc:
            msg = (
                f"scenario:call expects a JSON object after the marker, "
                f"got {tail[:120]!r}: {exc}"
            )
            raise ScenarioError(msg) from exc

        name = request.get("name")
        if not name:
            msg = f"scenario:call expects a 'name' key in its JSON, got {tail[:120]!r}"
            raise ScenarioError(msg)

        digest = hashlib.sha256(tail.encode("utf-8")).hexdigest()[:8]
        call = ToolCallSpec(
            call_id=f"call_{name}_{digest}",
            name=str(name),
            arguments=json.dumps(request.get("arguments", {})),
        )

        return Scenario(
            turns=[
                TurnScript(reasoning=f"I will call {name}", tool_calls=[call]),
                TurnScript(content=cls.CALL_ANSWER),
            ]
        )

    @classmethod
    def _words(cls, count: int, seed: int) -> str:
        words: list[str] = []
        for index in range(count):
            words.append(cls.LONG_WORDS[(index + seed) % len(cls.LONG_WORDS)])

        return " ".join(words)

    @classmethod
    def _long(cls, text: str) -> Scenario:
        """Длинный ход: id вызова несёт хеш сообщения, как у scenario:call."""
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
        call = ToolCallSpec(
            call_id=f"call_long_{digest}",
            name="stream_logs_usage",
            arguments="{}",
        )
        answer = (
            f"**Result.** {cls._words(cls.LONG_ANSWER_WORDS, 3)}\n\n"
            f"- {cls._words(8, 5)}\n- {cls._words(8, 7)}"
        )

        return Scenario(
            turns=[
                TurnScript(
                    reasoning=cls._words(cls.LONG_REASONING_WORDS, 0),
                    tool_calls=[call],
                ),
                TurnScript(content=answer),
            ]
        )

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

        @app.get(FakeRoute.HEALTH.value)
        async def health() -> dict[str, str]:
            return {"status": "ok"}

        @app.get(FakeRoute.PAGE.value)
        async def page() -> Response:
            """Страница для web-инструментов стенда: whitelist указывает сюда."""
            return Response(FakePage.HTML.value, media_type=FakePage.HTML.media_type)

        @app.get(FakeRoute.LINES.value)
        async def lines() -> Response:
            """Многострочный текст: окно строк и grep web-инструментов."""
            return Response(FakePage.LINES.value, media_type=FakePage.LINES.media_type)

        @app.post(FakeRoute.RESET.value)
        async def reset() -> dict[str, str]:
            """Сброс счётчика ходов и журнала: тест начинает с чистого листа."""
            self.turns_done.clear()
            self.requests.clear()
            return {"status": "ok"}

        @app.get(FakeRoute.REQUESTS.value)
        async def recorded() -> JSONResponse:
            """Журнал полных запросов провайдеру: тест сверяет параметры модели."""
            return JSONResponse({"requests": self.requests})

        @app.post(FakeRoute.COMPLETIONS.value)
        async def completions(request: Request) -> Response:
            payload = await request.json()
            self.requests.append(payload)
            text = self._last_user_text(payload)
            name = ScenarioName.of(text)
            key = self._turn_key(name, text)
            index = self.turns_done.get(key, 0)
            self.turns_done[key] = index + 1
            script = ScenarioBook.of(name, text).turn(index)

            if not payload.get("stream"):
                return JSONResponse(self._completion(script))

            return StreamingResponse(
                self._stream(script),
                media_type="text/event-stream",
            )

        return app

    @staticmethod
    def _turn_key(name: ScenarioName, text: str) -> str:
        """Ключ счётчика ходов: у продиктованного вызова — само сообщение.

        Иначе второй вызов в том же чате получил бы не tool_call, а ответ.
        """
        if name is ScenarioName.CALL:
            return text

        if name is ScenarioName.LONG:
            return text

        return name.value

    @staticmethod
    def _last_user_text(payload: dict[str, Any]) -> str:
        messages = payload.get("messages")
        if not messages:
            keys = sorted(payload)
            msg = (
                f"fake llm request: expected a non-empty 'messages' list, "
                f"got keys {keys}"
            )
            raise ScenarioError(msg)

        for message in reversed(messages):
            if message.get("role") != "user":
                continue

            content = message.get("content")
            if isinstance(content, str):
                return content

        roles = [message.get("role") for message in messages]
        msg = (
            f"fake llm request: no user message with string content among roles {roles}"
        )
        raise ScenarioError(msg)

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
