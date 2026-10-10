"""Фейковый провайдер модели: OpenAI-совместимый `/v1/chat/completions` и
нативный `/api/chat` ollama, с задержкой по токену и сценариями отказов.

Нужен интеграционным тестам ленты и агента: сценарий выбирается по тексту
последнего сообщения пользователя, поэтому тест диктует, какие шаги нарисует
ход, знает тайминг каждого токена и может заказать отказ сервера: обрыв
потока, статус, зависание, обрыв по длине, битые вызовы, ответ без учёта.

Ошибки: ScenarioError — в запросе нет сценария с таким именем или сценарий
собран неверно.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence, Set
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar, TypeAlias

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

__all__ = [
    "FailureName",
    "FakeLlmApp",
    "FakePage",
    "FakeRoute",
    "Played",
    "Scenario",
    "ScenarioError",
    "ScenarioName",
    "ToolCallSpec",
    "TurnScript",
]


class ScenarioError(Exception):
    """Запрошен сценарий, которого нет, либо сценарий собран неверно."""


class FakeRoute(StrEnum):
    """Маршруты фейкового сервера: провайдеры модели и страницы для web-тулов."""

    HEALTH = "/health"
    PAGE = "/page"
    LINES = "/lines"
    RESET = "/reset"
    REQUESTS = "/requests"
    COMPLETIONS = "/v1/chat/completions"
    OLLAMA_CHAT = "/api/chat"


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
    def find(cls, text: str) -> ScenarioName | None:
        """Маркер сценария в сообщении; None — маркера нет.

        Маркеры вложены друг в друга ('scenario:tool' — префикс
        'scenario:tool-error'), поэтому побеждает самый длинный.
        """
        ordered = sorted(cls, key=lambda name: len(name.value), reverse=True)
        for name in ordered:
            if name.value in text:
                return name

        return None

    @classmethod
    def of(cls, text: str) -> ScenarioName:
        """Маркер сценария в сообщении; без маркера — ошибка."""
        found = cls.find(text)
        if found is not None:
            return found

        markers = [name.value for name in cls]
        msg = (
            f"fake llm: none of the scenario markers {markers} in message {text[:80]!r}"
        )
        raise ScenarioError(msg)


class FailureName(StrEnum):
    """Отказы сервера, которые стенд разыгрывает поверх сценария.

    Маркер отказа стоит в том же сообщении пользователя, что и маркер
    сценария; без сценария отказ накладывается на `scenario:answer`.
    """

    CUT = "failure:cut"
    """Поток обрывается посреди текста: без финального чанка и без [DONE];
    ответ без потока приходит половиной тела."""

    STATUS_500 = "failure:status-500"
    STATUS_429 = "failure:status-429"
    OVERFLOW = "failure:overflow"
    """400 с текстом о переполнении окна контекста."""

    HANG = "failure:hang"
    """Сервер молчит hang_sec секунд, затем отвечает по сценарию."""

    LENGTH = "failure:length"
    """Текст приходит целиком, но finish_reason/done_reason = length."""

    LENGTH_ARGUMENTS = "failure:length-arguments"
    """Вызов инструмента оборван посреди JSON аргументов, причина — length."""

    BROKEN_ARGUMENTS = "failure:broken-arguments"
    """Аргументы вызова — не JSON, причина — tool_calls."""

    UNKNOWN_TOOL = "failure:unknown-tool"
    EMPTY_ID = "failure:empty-id"
    DUPLICATE_ID = "failure:duplicate-id"
    NO_USAGE = "failure:no-usage"
    EMPTY = "failure:empty"
    """Ответ без текста, без рассуждений и без вызовов."""

    THINK_TAGS = "failure:think-tags"
    """Рассуждения внутри текста тегами <think>, отдельного поля нет."""

    @classmethod
    def find(cls, text: str) -> FailureName | None:
        """Маркер отказа в сообщении; None — отказа не заказано. Длинный
        маркер побеждает: 'failure:length' — префикс 'failure:length-arguments'."""
        ordered = sorted(cls, key=lambda name: len(name.value), reverse=True)
        for name in ordered:
            if name.value in text:
                return name

        return None

    @property
    def answers_with_status(self) -> bool:
        return self in (
            FailureName.STATUS_500,
            FailureName.STATUS_429,
            FailureName.OVERFLOW,
        )

    @property
    def http_status(self) -> int:
        if self is FailureName.STATUS_500:
            return 500

        if self is FailureName.STATUS_429:
            return 429

        if self is FailureName.OVERFLOW:
            return 400

        msg = f"fake llm: failure {self.value} is not answered with a status"
        raise ScenarioError(msg)

    @property
    def error_text(self) -> str:
        if self is FailureName.OVERFLOW:
            return (
                "This model's maximum context length is 4096 tokens. However, your "
                "messages resulted in 5000 tokens. Please reduce the length of the "
                "messages."
            )

        if self is FailureName.STATUS_429:
            return "fake llm: rate limit exceeded, retry later"

        return "fake llm: internal server error"


@dataclass
class ToolCallSpec:
    """Вызов инструмента, который провайдер попросит выполнить.

    Подпись вызова intent обязательна у каждого инструмента приложения, и
    настоящая модель её заполняет; фейк ведёт себя так же — дописывает
    подпись, если сценарий её не задал. Вызов с literal=True уходит на
    провод как есть: так стенд отдаёт битые и оборванные аргументы.
    """

    call_id: str
    name: str
    arguments: str
    literal: bool = False

    INTENT_FIELD: ClassVar[str] = "intent"

    def __post_init__(self) -> None:
        if self.literal:
            return

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

    def arguments_wire(self) -> Any:
        """Аргументы для нативного формата ollama: объект, если они JSON-объект,
        иначе строка как есть — пусть клиент сам споткнётся о неё."""
        if self.literal:
            try:
                parsed = json.loads(self.arguments)
            except json.JSONDecodeError:
                return self.arguments

            return parsed

        return json.loads(self.arguments)


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


class FinishReason(StrEnum):
    """Причины конца ответа на проводе обоих протоколов."""

    STOP = "stop"
    TOOL_CALLS = "tool_calls"
    LENGTH = "length"

    def ollama(self) -> str:
        """done_reason ollama: вызовы инструментов приходят с причиной stop."""
        if self is FinishReason.TOOL_CALLS:
            return FinishReason.STOP.value

        return self.value


@dataclass(frozen=True)
class Played:
    """Что уходит на провод: ответ сценария, причина конца и заказанный отказ."""

    script: TurnScript
    finish: FinishReason
    failure: FailureName | None

    def has(self, failure: FailureName) -> bool:
        return self.failure is failure


@dataclass
class Scenario:
    """Последовательность ответов провайдера на один и тот же тред."""

    turns: Sequence[TurnScript]

    def turn(self, index: int) -> TurnScript:
        if index < len(self.turns):
            return self.turns[index]

        return self.turns[-1]

    def answered(self, called: Set[str]) -> int:
        """Сколько начальных ходов уже сыграно: их вызовы есть в разговоре.

        Номер ответа выводится из самого запроса, а не из счётчика: повтор
        запроса приложением (тайм-аут под нагрузкой) получает тот же ответ.
        """
        done = 0
        for script in self.turns:
            if not script.tool_calls:
                return done

            for call in script.tool_calls:
                if call.call_id not in called:
                    return done

            done += 1

        return done


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
        """Инструменты и аргументы диктует сам тест: `scenario:call {json}` —
        один вызов, `scenario:call [{json}, ...]` — несколько вызовов одним
        ответом, как их делает модель, связывая насосы каналами.

        Id вызова несёт хеш сообщения и номер в ответе: два вызова одного
        инструмента получают разные шаги ленты, а не перезаписывают один.
        """
        _, _, tail = text.partition(ScenarioName.CALL.value)
        try:
            request = json.loads(tail.strip())
        except json.JSONDecodeError as exc:
            msg = (
                f"scenario:call expects a JSON object or list after the marker, "
                f"got {tail[:120]!r}: {exc}"
            )
            raise ScenarioError(msg) from exc

        requests: list[Any] = [request]
        if isinstance(request, list):
            requests = list(request)

        digest = hashlib.sha256(tail.encode("utf-8")).hexdigest()[:8]
        calls: list[ToolCallSpec] = []
        for index, item in enumerate(requests):
            calls.append(cls._scripted_call(item, digest, index, tail))

        names: list[str] = []
        for call in calls:
            names.append(call.name)

        return Scenario(
            turns=[
                TurnScript(
                    reasoning=f"I will call {', '.join(names)}", tool_calls=calls
                ),
                TurnScript(content=cls.CALL_ANSWER),
            ]
        )

    @staticmethod
    def _scripted_call(item: Any, digest: str, index: int, tail: str) -> ToolCallSpec:
        if not isinstance(item, dict):
            msg = (
                f"scenario:call expects each call as a JSON object, got "
                f"{type(item).__name__} in {tail[:120]!r}"
            )
            raise ScenarioError(msg)

        name = item.get("name")
        if not name:
            msg = f"scenario:call expects a 'name' key in each call, got {tail[:120]!r}"
            raise ScenarioError(msg)

        call_id = f"call_{name}_{digest}"
        if index:
            call_id = f"{call_id}_{index}"

        return ToolCallSpec(
            call_id=call_id,
            name=str(name),
            arguments=json.dumps(item.get("arguments", {})),
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
            name="connection_list",
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

    @classmethod
    def _answer(cls) -> Scenario:
        """Ответ в три десятка токенов: поток дольше окна склейки ленты."""
        content = f"Here is a plain streamed answer: {cls._words(30, 3)}"

        return Scenario(turns=[TurnScript(content=content)])

    @classmethod
    def _thinking_answer(cls) -> Scenario:
        """Рассуждение и ответ по три десятка токенов: поток длится около
        секунды и дольше окна склейки ленты даже под нагрузкой хоста."""
        return Scenario(
            turns=[
                TurnScript(
                    reasoning=f"First I reason about the question: {cls._words(30, 1)}",
                    content=f"Then I answer the question: {cls._words(30, 2)}",
                )
            ]
        )

    @classmethod
    def _tool(cls) -> Scenario:
        call = ToolCallSpec(
            call_id="call_connection_list",
            name="connection_list",
            arguments="{}",
        )
        return Scenario(
            turns=[
                TurnScript(reasoning="I need the connections", tool_calls=[call]),
                TurnScript(content="The connections are above"),
            ]
        )

    @classmethod
    def _tool_error(cls) -> Scenario:
        call = ToolCallSpec(
            call_id="call_broken",
            name="send_file",
            arguments=json.dumps({"path": "no-such-file"}),
        )
        return Scenario(
            turns=[
                TurnScript(reasoning="I will send a missing file", tool_calls=[call]),
                TurnScript(content="The attachment failed"),
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


FailureBuilder: TypeAlias = Callable[[TurnScript, FailureName], Played]
"""Сборка ответа с отказом содержания из ответа сценария."""


class FailureBook:
    """Накладывает заказанный отказ на ответ сценария.

    Отказы содержания (битые вызовы, пустой ответ, теги) подменяют сам ответ;
    отказы провода (обрыв, статус, зависание, без учёта) ответ не трогают — их
    разыгрывает протокол.
    """

    TOOL: ClassVar[str] = "connection_list"
    UNKNOWN_TOOL: ClassVar[str] = "no_such_tool"
    DUPLICATE_ID: ClassVar[str] = "call_dup"
    CUT_ARGUMENTS: ClassVar[str] = '{"query": "unfini'
    BROKEN_ARGUMENTS: ClassVar[str] = "not json {"
    THINK_CONTENT: ClassVar[str] = (
        "<think>I reason inside the text</think>the answer after thinking"
    )

    def apply(self, script: TurnScript, failure: FailureName | None) -> Played:
        if failure is None:
            return Played(script, FinishReason(script.finish_reason()), None)

        builder = self._content_failures().get(failure)
        if builder is None:
            return Played(script, FinishReason(script.finish_reason()), failure)

        return builder(script, failure)

    def _content_failures(self) -> dict[FailureName, FailureBuilder]:
        return {
            FailureName.LENGTH: self._length,
            FailureName.LENGTH_ARGUMENTS: self._length_arguments,
            FailureName.BROKEN_ARGUMENTS: self._broken_arguments,
            FailureName.UNKNOWN_TOOL: self._unknown_tool,
            FailureName.EMPTY_ID: self._empty_id,
            FailureName.DUPLICATE_ID: self._duplicate_id,
            FailureName.EMPTY: self._empty,
            FailureName.THINK_TAGS: self._think_tags,
        }

    def _length(self, script: TurnScript, failure: FailureName) -> Played:
        return Played(script, FinishReason.LENGTH, failure)

    def _length_arguments(self, script: TurnScript, failure: FailureName) -> Played:
        call = ToolCallSpec("call_cut", self.TOOL, self.CUT_ARGUMENTS, literal=True)
        cut = TurnScript(reasoning="I will call a tool", tool_calls=[call])

        return Played(cut, FinishReason.LENGTH, failure)

    def _broken_arguments(self, script: TurnScript, failure: FailureName) -> Played:
        call = ToolCallSpec(
            "call_broken_args", self.TOOL, self.BROKEN_ARGUMENTS, literal=True
        )

        return Played(TurnScript(tool_calls=[call]), FinishReason.TOOL_CALLS, failure)

    def _unknown_tool(self, script: TurnScript, failure: FailureName) -> Played:
        call = ToolCallSpec("call_unknown", self.UNKNOWN_TOOL, "{}")

        return Played(TurnScript(tool_calls=[call]), FinishReason.TOOL_CALLS, failure)

    def _empty_id(self, script: TurnScript, failure: FailureName) -> Played:
        call = ToolCallSpec("", self.TOOL, "{}")

        return Played(TurnScript(tool_calls=[call]), FinishReason.TOOL_CALLS, failure)

    def _duplicate_id(self, script: TurnScript, failure: FailureName) -> Played:
        first = ToolCallSpec(self.DUPLICATE_ID, self.TOOL, "{}")
        second = ToolCallSpec(self.DUPLICATE_ID, self.TOOL, '{"query": "second"}')
        twice = TurnScript(tool_calls=[first, second])

        return Played(twice, FinishReason.TOOL_CALLS, failure)

    def _empty(self, script: TurnScript, failure: FailureName) -> Played:
        return Played(TurnScript(), FinishReason.STOP, failure)

    def _think_tags(self, script: TurnScript, failure: FailureName) -> Played:
        tagged = TurnScript(content=self.THINK_CONTENT)

        return Played(tagged, FinishReason.STOP, failure)


class TokenSplit:
    """Делит текст на токены-слова с ведущим пробелом, как их шлёт поток."""

    def tokens(self, text: str) -> Iterator[str]:
        if not text:
            return

        for index, word in enumerate(text.split(" ")):
            if index:
                yield f" {word}"
                continue

            yield word

    def half(self, text: str) -> Iterator[str]:
        """Первая половина токенов: столько уходит до обрыва потока."""
        tokens = list(self.tokens(text))
        keep = max(1, len(tokens) // 2)

        yield from tokens[:keep]


class OpenAiWire:
    """Ответы протокола /v1/chat/completions: тело целиком и SSE-поток."""

    MEDIA_STREAM: ClassVar[str] = "text/event-stream"
    MEDIA_JSON: ClassVar[str] = "application/json"
    PROMPT_TOKENS: ClassVar[int] = 11
    COMPLETION_TOKENS: ClassVar[int] = 7

    def __init__(self, model: str, token_delay_sec: float) -> None:
        self._model = model
        self._delay = token_delay_sec
        self._split = TokenSplit()

    def error(self, failure: FailureName) -> Response:
        body = {
            "error": {
                "message": failure.error_text,
                "type": "invalid_request_error",
                "code": failure.value,
            }
        }

        return JSONResponse(body, status_code=failure.http_status)

    def completion(self, played: Played) -> Response:
        """Ответ без стрима: текст, рассуждения и вызовы приходят разом."""
        body = json.dumps(self._completion(played), ensure_ascii=False).encode()
        if played.has(FailureName.CUT):
            body = body[: len(body) // 2]

        return Response(body, media_type=self.MEDIA_JSON)

    def stream(self, played: Played) -> Response:
        return StreamingResponse(self._stream(played), media_type=self.MEDIA_STREAM)

    def _completion(self, played: Played) -> dict[str, Any]:
        script = played.script
        message: dict[str, Any] = {"role": "assistant", "content": script.content}
        if script.reasoning:
            message["reasoning"] = script.reasoning

        calls: list[dict[str, Any]] = []
        for index, call in enumerate(script.tool_calls):
            calls.append(self._call(index, call))
        if calls:
            message["tool_calls"] = calls

        body: dict[str, Any] = {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "created": 1,
            "model": self._model,
            "choices": [
                {"index": 0, "message": message, "finish_reason": played.finish.value}
            ],
        }
        if not played.has(FailureName.NO_USAGE):
            body["usage"] = self._usage()

        return body

    async def _stream(self, played: Played) -> AsyncIterator[bytes]:
        for chunk in self._chunks(played):
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
            await asyncio.sleep(self._delay)

        if played.has(FailureName.CUT):
            return

        yield b"data: [DONE]\n\n"

    def _chunks(self, played: Played) -> Iterator[dict[str, Any]]:
        script = played.script
        for token in self._split.tokens(script.reasoning):
            yield self._delta({"role": "assistant", "content": "", "reasoning": token})

        if played.has(FailureName.CUT):
            for token in self._split.half(script.content):
                yield self._delta({"role": "assistant", "content": token})

            return

        for token in self._split.tokens(script.content):
            yield self._delta({"role": "assistant", "content": token})

        for index, call in enumerate(script.tool_calls):
            delta = {
                "role": "assistant",
                "content": "",
                "tool_calls": [self._call(index, call)],
            }
            yield self._delta(delta)

        final = self._delta({"role": "assistant", "content": ""}, played.finish.value)
        if not played.has(FailureName.NO_USAGE):
            final["usage"] = self._usage()

        yield final

    def _call(self, index: int, call: ToolCallSpec) -> dict[str, Any]:
        return {
            "index": index,
            "id": call.call_id,
            "type": "function",
            "function": {"name": call.name, "arguments": call.arguments},
        }

    def _usage(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.PROMPT_TOKENS,
            "completion_tokens": self.COMPLETION_TOKENS,
            "total_tokens": self.PROMPT_TOKENS + self.COMPLETION_TOKENS,
        }

    def _delta(
        self, delta: dict[str, Any], finish_reason: str | None = None
    ) -> dict[str, Any]:
        return {
            "id": "chatcmpl-fake",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": self._model,
            "choices": [
                {"index": 0, "delta": delta, "finish_reason": finish_reason},
            ],
        }


class OllamaWire:
    """Ответы нативного протокола /api/chat: тело целиком и NDJSON-поток."""

    MEDIA_STREAM: ClassVar[str] = "application/x-ndjson"
    MEDIA_JSON: ClassVar[str] = "application/json"
    CREATED_AT: ClassVar[str] = "2026-01-01T00:00:00Z"
    PROMPT_EVAL: ClassVar[int] = 11
    EVAL: ClassVar[int] = 7

    def __init__(self, model: str, token_delay_sec: float) -> None:
        self._model = model
        self._delay = token_delay_sec
        self._split = TokenSplit()

    def error(self, failure: FailureName) -> Response:
        return JSONResponse(
            {"error": failure.error_text}, status_code=failure.http_status
        )

    def completion(self, played: Played) -> Response:
        script = played.script
        message = self._message(script.content, script.reasoning, script.tool_calls)
        chunk = self._chunk(message, done=True)
        chunk["done_reason"] = played.finish.ollama()
        if not played.has(FailureName.NO_USAGE):
            chunk.update(self._counts())

        body = json.dumps(chunk, ensure_ascii=False).encode()
        if played.has(FailureName.CUT):
            body = body[: len(body) // 2]

        return Response(body, media_type=self.MEDIA_JSON)

    def stream(self, played: Played) -> Response:
        return StreamingResponse(self._stream(played), media_type=self.MEDIA_STREAM)

    async def _stream(self, played: Played) -> AsyncIterator[bytes]:
        for chunk in self._chunks(played):
            yield (json.dumps(chunk, ensure_ascii=False) + "\n").encode()
            await asyncio.sleep(self._delay)

    def _chunks(self, played: Played) -> Iterator[dict[str, Any]]:
        script = played.script
        for token in self._split.tokens(script.reasoning):
            yield self._chunk(self._message("", token, ()), done=False)

        if played.has(FailureName.CUT):
            for token in self._split.half(script.content):
                yield self._chunk(self._message(token, "", ()), done=False)

            return

        for token in self._split.tokens(script.content):
            yield self._chunk(self._message(token, "", ()), done=False)

        if script.tool_calls:
            yield self._chunk(self._message("", "", script.tool_calls), done=False)

        final = self._chunk(self._message("", "", ()), done=True)
        final["done_reason"] = played.finish.ollama()
        if not played.has(FailureName.NO_USAGE):
            final.update(self._counts())

        yield final

    def _message(
        self, content: str, thinking: str, calls: Sequence[ToolCallSpec]
    ) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": content}
        if thinking:
            message["thinking"] = thinking

        wired: list[dict[str, Any]] = []
        for call in calls:
            wired.append(
                {
                    "id": call.call_id,
                    "function": {
                        "name": call.name,
                        "arguments": call.arguments_wire(),
                    },
                }
            )
        if wired:
            message["tool_calls"] = wired

        return message

    def _chunk(self, message: dict[str, Any], done: bool) -> dict[str, Any]:
        return {
            "model": self._model,
            "created_at": self.CREATED_AT,
            "message": message,
            "done": done,
        }

    def _counts(self) -> dict[str, int]:
        return {"prompt_eval_count": self.PROMPT_EVAL, "eval_count": self.EVAL}


@dataclass
class FakeLlmApp:
    """ASGI-приложение провайдера: сценарии по маркеру, отказы по маркеру,
    журнал принятых запросов обоих протоколов."""

    token_delay_sec: float = 0.02
    model: str = "fake-model"
    hang_sec: float = 3600.0
    requests: list[dict[str, Any]] = field(default_factory=list)
    _openai: OpenAiWire = field(init=False, repr=False)
    _ollama: OllamaWire = field(init=False, repr=False)
    _failures: FailureBook = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._openai = OpenAiWire(self.model, self.token_delay_sec)
        self._ollama = OllamaWire(self.model, self.token_delay_sec)
        self._failures = FailureBook()

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
            """Сброс журнала запросов: тест начинает с чистого листа."""
            self.requests.clear()
            return {"status": "ok"}

        @app.get(FakeRoute.REQUESTS.value)
        async def recorded() -> JSONResponse:
            """Журнал полных запросов провайдеру: тест сверяет параметры модели."""
            return JSONResponse({"requests": self.requests})

        @app.post(FakeRoute.COMPLETIONS.value)
        async def completions(request: Request) -> Response:
            return await self._play(request, self._openai)

        @app.post(FakeRoute.OLLAMA_CHAT.value)
        async def ollama_chat(request: Request) -> Response:
            return await self._play(request, self._ollama)

        return app

    async def _play(self, request: Request, wire: OpenAiWire | OllamaWire) -> Response:
        """Общий ход обоих протоколов: сценарий и отказ по последнему
        сообщению пользователя, ответ — в формате протокола."""
        payload = await request.json()
        self.requests.append(payload)

        text = self._last_user_text(payload)
        failure = FailureName.find(text)
        played = self._played(payload, text, failure)

        if failure is not None and failure.answers_with_status:
            return wire.error(failure)

        if failure is FailureName.HANG:
            await asyncio.sleep(self.hang_sec)

        if not payload.get("stream"):
            return wire.completion(played)

        return wire.stream(played)

    def _played(
        self, payload: Mapping[str, Any], text: str, failure: FailureName | None
    ) -> Played:
        name = ScenarioName.find(text)
        if name is None and failure is None:
            name = ScenarioName.of(text)

        if name is None:
            name = ScenarioName.ANSWER

        scenario = ScenarioBook.of(name, text)
        script = scenario.turn(scenario.answered(self._called_ids(payload)))

        return self._failures.apply(script, failure)

    @staticmethod
    def _called_ids(payload: Mapping[str, Any]) -> set[str]:
        """Id вызовов, которые ассистент уже сделал после последнего сообщения
        пользователя: по ним сценарий узнаёт, какой ход отвечать."""
        called: set[str] = set()
        messages = payload.get("messages")
        if not messages:
            return called

        for message in reversed(messages):
            if message.get("role") == "user":
                return called

            if message.get("role") != "assistant":
                continue

            if calls := message.get("tool_calls"):
                for call in calls:
                    called.add(str(call.get("id")))

        return called

    @staticmethod
    def _last_user_text(payload: Mapping[str, Any]) -> str:
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


def serve(host: str, port: int, token_delay_sec: float) -> None:
    """Запуск провайдера отдельным процессом."""
    app = FakeLlmApp(token_delay_sec=token_delay_sec)
    uvicorn.run(app.asgi(), host=host, port=port, log_level="warning")
