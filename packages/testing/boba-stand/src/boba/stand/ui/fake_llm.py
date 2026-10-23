"""Фейковый провайдер модели: OpenAI-совместимый `/v1/chat/completions` и
нативный `/api/chat` ollama, с задержкой по токену и сценариями отказов.

Нужен интеграционным тестам ленты и агента: сценарий выбирается по тексту
последнего сообщения пользователя, поэтому тест диктует, какие шаги нарисует
ход, знает тайминг каждого токена и может заказать отказ сервера: обрыв
потока, статус, зависание, обрыв по длине, битые вызовы, ответ без учёта.
Инструмент готового сценария назван именем на сервере; на провод уходит
полное имя инструмента из списка `tools` запроса (OfferedTools) — как
модель, стенд зовёт то, что ему предложили. В `scenario:call` имя диктует
тест, и оно уходит как есть.
Запрос пересказа агента (сжатие истории) стенд узнаёт по инструкции в
последнем сообщении и отвечает по маркеру `summary:*`; учёт токенов либо
постоянный, либо считается по размеру запроса и ответа.

Книга ответов (ReplyBook) — ответы, которые тест заранее положил под точным
текстом вопроса пользователя (`PUT /replies`): вопрос в ленте остаётся
обычным текстом, а ход книги умеет встать на паузу посреди потока до
`POST /replies/release`. Вопрос, которого нет в книге, идёт по маркерам; без
маркера стенд отказывает 400 и называет ближайшие вопросы книги.

Ошибки:
ScenarioError — в запросе нет сценария с таким именем или сценарий собран неверно.
ReplyConflictError — один вопрос пришёл в книгу с разными ответами.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import unicodedata
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, ClassVar, Literal, TypeAlias

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, JsonValue, RootModel, ValidationError

from boba.agent.records import LinkText
from boba.agent.tags import AgentTag
from boba.core.mcp_names import McpToolNames

__all__ = [
    "FailureName",
    "FakeLlmApp",
    "FakePage",
    "FakeRoute",
    "Played",
    "Reply",
    "ReplyBookClient",
    "ReplyCall",
    "ReplyConflictError",
    "ReplyTurn",
    "Scenario",
    "ScenarioError",
    "ScenarioName",
    "SummaryName",
    "ToolCallSpec",
    "TurnScript",
    "UsageCounts",
]


class ScenarioError(Exception):
    """Запрошен сценарий, которого нет, либо сценарий собран неверно."""


class ReplyConflictError(ScenarioError):
    """Один и тот же вопрос пришёл в книгу ответов с разными ответами."""


class FakeRoute(StrEnum):
    """Маршруты фейкового сервера: провайдеры модели и страницы для web-тулов."""

    HEALTH = "/health"
    PAGE = "/page"
    LINES = "/lines"
    RESET = "/reset"
    REQUESTS = "/requests"
    REPLIES = "/replies"
    RELEASE = "/replies/release"
    HELD = "/replies/held"
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

    LOOP = "scenario:loop"
    """Вызов инструмента, который модель повторяет после каждого результата:
    ход без предела шагов не кончится."""

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
    сценария; без сценария отказ накладывается на `scenario:answer`. NONE —
    ход без отказа: им отвечает поиск без маркера и его держит ход книги.
    """

    NONE = "failure:none"
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
    def find(cls, text: str) -> FailureName:
        """Маркер отказа в сообщении; NONE — отказа не заказано. Длинный
        маркер побеждает: 'failure:length' — префикс 'failure:length-arguments'."""
        ordered = sorted(cls, key=lambda name: len(name.value), reverse=True)
        for name in ordered:
            if name is cls.NONE:
                continue

            if name.value in text:
                return name

        return cls.NONE

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


class SummaryName(StrEnum):
    """Как стенд отвечает на запрос пересказа агента.

    Маркер лежит в любом сообщении пользователя запроса; побеждает ближайший
    к концу. Без маркера — OK: пересказ с блоком <summary>, в котором
    повторены строки с маркерами стенда, чтобы следующие запросы шли по тем же
    сценариям.
    """

    OK = "summary:ok"
    TOOL_CALL = "summary:tool-call"
    """Вместо текста — вызов инструмента."""

    NO_BLOCK = "summary:no-block"
    """Текст без блока <summary>."""

    EMPTY = "summary:empty"
    LONG = "summary:long"
    """Пересказ длиннее любого разумного отрезка разговора."""

    LENGTH = "summary:length"
    """Пересказ оборван по длине: finish_reason = length."""

    OVERFLOW = "summary:overflow"
    """400 с текстом о переполнении окна, пока маркер виден в запросе."""

    HANG = "summary:hang"
    """Сервер молчит hang_sec секунд, затем отвечает OK."""

    @classmethod
    def find(cls, text: str) -> SummaryName | None:
        ordered = sorted(cls, key=lambda name: len(name.value), reverse=True)
        for name in ordered:
            if name.value in text:
                return name

        return None


@dataclass(frozen=True)
class UsageCounts:
    """Учёт токенов ответа на проводе."""

    prompt: int
    completion: int


@dataclass
class ToolCallSpec:
    """Вызов инструмента, который провайдер попросит выполнить.

    Подпись вызова intent обязательна у каждого инструмента приложения, и
    настоящая модель её заполняет; фейк ведёт себя так же — дописывает
    подпись, если сценарий её не задал. Вызов с literal=True уходит на
    провод как есть: так стенд отдаёт битые и оборванные аргументы. Вызов
    с dictated=True назван тестом (`scenario:call`): имя уходит как есть,
    стенд не ищет его среди предложенных инструментов. Подпись называет
    инструмент без сервера — так же, как его подписывает шаг ленты. Вызов с
    unsigned=True идёт без подписи: у инструментов чужих серверов поля intent
    в схеме нет, и лишний аргумент сервер отвергает.
    """

    call_id: str
    name: str
    arguments: str
    literal: bool = False
    dictated: bool = False
    unsigned: bool = False
    names: McpToolNames = field(default_factory=McpToolNames, repr=False, compare=False)

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

        if self.unsigned:
            parsed.pop(self.INTENT_FIELD, None)
        elif self.INTENT_FIELD not in parsed:
            tool = self.names.tool_of(self.name)
            parsed[self.INTENT_FIELD] = f"stand call of {tool}"

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
    failure: FailureName

    def has(self, failure: FailureName) -> bool:
        return self.failure is failure


class OfferedTools:
    """Инструменты, которые запрос предлагает модели: по ним стенд называет
    вызовы сценария так, как их видит агент.

    Готовый сценарий знает инструмент по имени на сервере
    (`connection_list`), а агент предлагает его под полным именем сервера
    MCP. Имя, предложенное как есть, остаётся собой; иначе берётся
    единственный предложенный инструмент с таким именем на сервере; не
    найден или найден у нескольких серверов — имя уходит как есть, и агент
    ответит, что такого инструмента нет. Вызов, названный тестом
    (`scenario:call`), не трогается: тест диктует имя целиком. Создаётся
    приложением стенда на каждый запрос.
    """

    def __init__(self, payload: Mapping[str, Any], names: McpToolNames) -> None:
        self._names = names
        self._offered = list(self._listed(payload))

    def named(self, tool: str) -> str:
        if tool in self._offered:
            return tool

        found: list[str] = []
        for offered in self._offered:
            if self._names.tool_of(offered) == tool:
                found.append(offered)

        if len(found) != 1:
            return tool

        return found[0]

    def script(self, script: TurnScript) -> TurnScript:
        """Тот же ответ с вызовами под предложенными именами."""
        calls: list[ToolCallSpec] = []
        for call in script.tool_calls:
            if call.dictated:
                calls.append(call)
                continue

            calls.append(replace(call, name=self.named(call.name)))

        return replace(script, tool_calls=calls)

    def scenario(self, scenario: Scenario) -> Scenario:
        turns: list[TurnScript] = []
        for turn in scenario.turns:
            turns.append(self.script(turn))

        return Scenario(turns=turns)

    def played(self, played: Played) -> Played:
        return replace(played, script=self.script(played.script))

    @staticmethod
    def _listed(payload: Mapping[str, Any]) -> Iterator[str]:
        tools = payload.get("tools")
        if not tools:
            return

        for tool in tools:
            function = tool.get("function")
            if not function:
                continue

            name = function.get("name")
            if isinstance(name, str):
                yield name


@dataclass
class Scenario:
    """Последовательность ответов провайдера на один и тот же тред."""

    turns: Sequence[TurnScript]

    def turn(self, index: int) -> TurnScript:
        if index < len(self.turns):
            return self.turns[index]

        return self.turns[-1]

    def answered(self, called: Sequence[str]) -> int:
        """Сколько начальных ходов уже сыграно: их вызовы есть в разговоре.

        Номер ответа выводится из самого запроса, а не из счётчика: повтор
        запроса приложением (тайм-аут под нагрузкой) получает тот же ответ.
        Вызовы узнаются по именам инструментов с кратностью, не по
        идентификаторам: клиент вправе выдавать вызовам свои идентификаторы.
        """
        remaining = list(called)
        done = 0
        for script in self.turns:
            if not script.tool_calls:
                return done

            for call in script.tool_calls:
                if call.name not in remaining:
                    return done

                remaining.remove(call.name)

            done += 1

        return done


class SummaryBook:
    """Ответы на запрос пересказа агента."""

    INSTRUCTION_MARK: ClassVar[str] = (
        "an <analysis> block followed by a <summary> block"
    )
    """Фраза инструкции пересказа, по которой стенд узнаёт запрос."""

    LONG_CHARS: ClassVar[int] = 60_000
    TOOL: ClassVar[str] = "lookup"

    def is_summary_request(self, payload: Mapping[str, Any]) -> bool:
        messages = payload.get("messages")
        if not messages:
            return False

        for message in reversed(messages):
            if message.get("role") != "user":
                continue

            content = message.get("content")
            if not isinstance(content, str):
                continue

            return self.INSTRUCTION_MARK in content

        return False

    def name(self, payload: Mapping[str, Any]) -> SummaryName:
        for text in reversed(self._user_texts(payload)):
            found = SummaryName.find(text)
            if found is not None:
                return found

        return SummaryName.OK

    def played(self, payload: Mapping[str, Any], name: SummaryName) -> Played:
        if name is SummaryName.TOOL_CALL:
            call = ToolCallSpec("call_summary_tool", self.TOOL, "{}")
            return Played(
                TurnScript(tool_calls=[call]), FinishReason.TOOL_CALLS, FailureName.NONE
            )

        if name is SummaryName.NO_BLOCK:
            script = TurnScript(
                content="<analysis>only analysis</analysis> and no summary block"
            )
            return Played(script, FinishReason.STOP, FailureName.NONE)

        if name is SummaryName.EMPTY:
            return Played(TurnScript(), FinishReason.STOP, FailureName.NONE)

        if name is SummaryName.LONG:
            script = TurnScript(content=f"<summary>{'x' * self.LONG_CHARS}</summary>")
            return Played(script, FinishReason.STOP, FailureName.NONE)

        finish = FinishReason.STOP
        if name is SummaryName.LENGTH:
            finish = FinishReason.LENGTH

        return Played(
            TurnScript(content=self._ok_text(payload)), finish, FailureName.NONE
        )

    def _ok_text(self, payload: Mapping[str, Any]) -> str:
        lines: list[str] = []
        for text in self._user_texts(payload):
            for line in text.splitlines():
                unmarked = FailureName.find(line) is FailureName.NONE
                if ScenarioName.find(line) is None and unmarked:
                    if SummaryName.find(line) is None:
                        continue

                lines.append(f"    - {line.strip()}")

        listed = "\n".join(lines)

        return (
            "<analysis>\nThe stand model read the conversation and lists what the "
            "user asked for.\n</analysis>\n\n<summary>\n1. Primary Request and "
            "Intent:\n   The user drove the stand through the scenario markers "
            f"listed below.\n\n6. All user messages:\n{listed}\n</summary>"
        )

    @staticmethod
    def _user_texts(payload: Mapping[str, Any]) -> list[str]:
        texts: list[str] = []
        messages = payload.get("messages")
        if not messages:
            return texts

        for message in messages:
            if message.get("role") != "user":
                continue

            content = message.get("content")
            if isinstance(content, str):
                texts.append(content)

        return texts


class UsageBook:
    """Учёт токенов ответа: постоянные числа либо оценка по знакам запроса
    и ответа, как у настоящего провайдера."""

    PROMPT: ClassVar[int] = 11
    COMPLETION: ClassVar[int] = 7
    CHARS_PER_TOKEN: ClassVar[int] = 4

    def __init__(self, estimate: bool) -> None:
        self._estimate = estimate

    def of(self, payload: Mapping[str, Any], script: TurnScript) -> UsageCounts:
        if not self._estimate:
            return UsageCounts(prompt=self.PROMPT, completion=self.COMPLETION)

        prompt_chars = 0
        for message in payload.get("messages") or []:
            prompt_chars += len(json.dumps(message, ensure_ascii=False))

        completion_chars = len(script.reasoning) + len(script.content)
        for call in script.tool_calls:
            completion_chars += len(call.name) + len(call.arguments)

        return UsageCounts(
            prompt=-(-prompt_chars // self.CHARS_PER_TOKEN),
            completion=-(-completion_chars // self.CHARS_PER_TOKEN),
        )


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

        if name is ScenarioName.LOOP:
            return cls._loop(text)

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

    @classmethod
    def _loop(cls, text: str) -> Scenario:
        """Вызов по JSON как у `scenario:call`, но без завершающего ответа:
        последний ход сценария повторяется, пока клиент шлёт результаты."""
        _, _, tail = text.partition(ScenarioName.LOOP.value)
        try:
            request = json.loads(tail.strip())
        except json.JSONDecodeError as exc:
            msg = (
                f"scenario:loop expects a JSON object after the marker, "
                f"got {tail[:120]!r}: {exc}"
            )
            raise ScenarioError(msg) from exc

        digest = hashlib.sha256(tail.encode("utf-8")).hexdigest()[:8]
        call = cls._scripted_call(request, digest, 0, tail)

        return Scenario(
            turns=[
                TurnScript(
                    reasoning=f"I will call {call.name} again", tool_calls=[call]
                )
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
            dictated=True,
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

    def apply(self, script: TurnScript, failure: FailureName) -> Played:
        if failure is FailureName.NONE:
            return Played(script, FinishReason(script.finish_reason()), failure)

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
    REJECTED_CODE: ClassVar[str] = "no_reply"

    def __init__(self, model: str) -> None:
        self._model = model
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

    def rejected(self, message: str) -> Response:
        """Отказ 400: на вопрос у стенда нет ни ответа книги, ни маркера."""
        body = {
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "code": self.REJECTED_CODE,
            }
        }

        return JSONResponse(body, status_code=httpx.codes.BAD_REQUEST)

    def completion(self, played: Played, usage: UsageCounts) -> Response:
        """Ответ без стрима: текст, рассуждения и вызовы приходят разом."""
        body = json.dumps(self._completion(played, usage), ensure_ascii=False).encode()
        if played.has(FailureName.CUT):
            body = body[: len(body) // 2]

        return Response(body, media_type=self.MEDIA_JSON)

    def stream(self, played: Played, usage: UsageCounts, pace: StreamPace) -> Response:
        return StreamingResponse(
            self._stream(played, usage, pace), media_type=self.MEDIA_STREAM
        )

    def _completion(self, played: Played, usage: UsageCounts) -> dict[str, Any]:
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
            body["usage"] = self._usage(usage)

        return body

    async def _stream(
        self, played: Played, usage: UsageCounts, pace: StreamPace
    ) -> AsyncIterator[bytes]:
        for index, chunk in enumerate(self._chunks(played, usage, pace)):
            await pace.before(index)
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
            await asyncio.sleep(pace.delay_sec)

        if played.has(FailureName.CUT):
            return

        yield b"data: [DONE]\n\n"

    def _chunks(
        self, played: Played, usage: UsageCounts, pace: StreamPace
    ) -> Iterator[dict[str, Any]]:
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
            for part in self._call_deltas(index, call, pace.argument_tokens):
                delta = {"role": "assistant", "content": "", "tool_calls": [part]}
                yield self._delta(delta)

        final = self._delta({"role": "assistant", "content": ""}, played.finish.value)
        if not played.has(FailureName.NO_USAGE):
            final["usage"] = self._usage(usage)

        yield final

    def _call(self, index: int, call: ToolCallSpec) -> dict[str, Any]:
        return {
            "index": index,
            "id": call.call_id,
            "type": "function",
            "function": {"name": call.name, "arguments": call.arguments},
        }

    def _call_deltas(
        self, index: int, call: ToolCallSpec, argument_tokens: bool
    ) -> Iterator[dict[str, Any]]:
        """Дельты одного вызова: целиком либо, как у настоящей модели,
        имя с первым куском аргументов и далее по куску на кадр."""
        tokens = list(self._split.tokens(call.arguments))
        tail = tokens[1:]
        if not argument_tokens or not tail:
            yield self._call(index, call)
            return

        head = self._call(index, call)
        head["function"]["arguments"] = tokens[0]
        yield head

        for token in tail:
            yield {"index": index, "function": {"arguments": token}}

    @staticmethod
    def _usage(usage: UsageCounts) -> dict[str, int]:
        return {
            "prompt_tokens": usage.prompt,
            "completion_tokens": usage.completion,
            "total_tokens": usage.prompt + usage.completion,
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

    def __init__(self, model: str) -> None:
        self._model = model
        self._split = TokenSplit()

    def error(self, failure: FailureName) -> Response:
        return JSONResponse(
            {"error": failure.error_text}, status_code=failure.http_status
        )

    def rejected(self, message: str) -> Response:
        """Отказ 400: на вопрос у стенда нет ни ответа книги, ни маркера."""
        return JSONResponse({"error": message}, status_code=httpx.codes.BAD_REQUEST)

    def completion(self, played: Played, usage: UsageCounts) -> Response:
        script = played.script
        message = self._message(script.content, script.reasoning, script.tool_calls)
        chunk = self._chunk(message, done=True)
        chunk["done_reason"] = played.finish.ollama()
        if not played.has(FailureName.NO_USAGE):
            chunk.update(self._counts(usage))

        body = json.dumps(chunk, ensure_ascii=False).encode()
        if played.has(FailureName.CUT):
            body = body[: len(body) // 2]

        return Response(body, media_type=self.MEDIA_JSON)

    def stream(self, played: Played, usage: UsageCounts, pace: StreamPace) -> Response:
        return StreamingResponse(
            self._stream(played, usage, pace), media_type=self.MEDIA_STREAM
        )

    async def _stream(
        self, played: Played, usage: UsageCounts, pace: StreamPace
    ) -> AsyncIterator[bytes]:
        for index, chunk in enumerate(self._chunks(played, usage)):
            await pace.before(index)
            yield (json.dumps(chunk, ensure_ascii=False) + "\n").encode()
            await asyncio.sleep(pace.delay_sec)

    def _chunks(self, played: Played, usage: UsageCounts) -> Iterator[dict[str, Any]]:
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
            final.update(self._counts(usage))

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

    @staticmethod
    def _counts(usage: UsageCounts) -> dict[str, int]:
        return {"prompt_eval_count": usage.prompt, "eval_count": usage.completion}


class ReplyCall(BaseModel):
    """Вызов инструмента в ходе книги ответов: имя, каким его видит модель
    (`diagram_save` или полное `mcp__standard__blocks`), аргументы и подпись
    вызова. Подпись intent — поле схемы инструментов boba, настоящая модель
    её заполняет; у инструмента чужого сервера этого поля нет, и подпись не
    задаётся (None) — лишний аргумент такой сервер отвергает."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str = Field(min_length=1)
    arguments: Mapping[str, JsonValue]
    intent: str | None = Field(default=None, min_length=1)


class ReplyTurn(BaseModel):
    """Один ответ модели в ходе книги: рассуждение, текст, вызовы и отказ.

    hold ставит поток на паузу после hold_after_tokens токенов (сначала
    рассуждения, затем текст; 0 — ни одного) до `POST /replies/release`:
    тест снимает кадр «поток идёт» ровно с этим числом токенов.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reasoning: str = ""
    content: str = ""
    calls: Sequence[ReplyCall] = ()
    failure: FailureName = FailureName.NONE
    hold: bool = False
    hold_after_tokens: int = Field(default=0, ge=0)


class Reply(BaseModel):
    """Ответ книги на один вопрос: ходы модели по порядку и темп потока.

    Какой ход отвечать, стенд выводит из самого запроса — по вызовам, уже
    сыгранным после вопроса, как у сценариев по маркеру.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    question: str = Field(min_length=1)
    turns: Sequence[ReplyTurn] = Field(min_length=1)
    token_delay_sec: float = Field(default=0.0, ge=0)
    stream_arguments: bool = False
    """Аргументы вызовов идут кусками-токенами по кадру потока (OpenAI)."""


class ReplyList(RootModel[Sequence[Reply]]):
    """Тело `PUT /replies`: книга целиком, заменяет прежнюю."""


class ReplyRelease(BaseModel):
    """Тело `POST /replies/release`: вопрос, чей поток отпустить."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    question: str = Field(min_length=1)


class ReplyHeld(BaseModel):
    """Ответ `GET /replies/held`: вопросы, чьи потоки сейчас стоят на паузе."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    held: Sequence[str]


class ReplyQuestions:
    """Вопрос пользователя в запросе модели — ключ книги ответов.

    Берётся последнее сообщение пользователя с текстом; у сообщения частями —
    текст частей `text`. Агент дописывает в то же сообщение свои служебные
    блоки `<system-reminder>` и ссылки на вложения вопроса (LinkText),
    поэтому ключ — только набранное пользователем: блоки и ссылки
    вырезаются, остаток проходит strip и NFC. Тем же правилом приводится
    вопрос, который кладёт в книгу тест.
    """

    FORM: ClassVar[Literal["NFC"]] = "NFC"
    TEXT_PART: ClassVar[str] = "text"
    PART_JOIN: ClassVar[str] = "\n\n"
    LINK: ClassVar[re.Pattern[str]] = re.compile(
        re.escape(LinkText.TEMPLATE.value)
        .replace(re.escape("{name}"), r"[^\]]*")
        .replace(re.escape("{uri}"), r"[^)]*")
    )
    """Ссылка на вложение вопроса в тексте, каким его видит модель."""

    def normal(self, text: str) -> str:
        return unicodedata.normalize(self.FORM, text.strip())

    def of(self, payload: Mapping[str, Any]) -> str:
        """Вопрос последнего сообщения пользователя с текстом; пусто — его нет."""
        messages = payload.get("messages")
        if not messages:
            return ""

        for message in reversed(messages):
            if message.get("role") != "user":
                continue

            typed = self._typed(self._text(message.get("content")))
            question = self.normal(typed)
            if question:
                return question

        return ""

    def _text(self, content: object) -> str:
        if isinstance(content, str):
            return content

        if not isinstance(content, list):
            return ""

        return self.PART_JOIN.join(self._text_parts(content))

    def _text_parts(self, parts: Sequence[object]) -> Iterator[str]:
        for part in parts:
            if not isinstance(part, dict):
                continue

            if part.get("type") != self.TEXT_PART:
                continue

            text = part.get(self.TEXT_PART)
            if isinstance(text, str):
                yield text

    def _typed(self, text: str) -> str:
        """Текст без служебных блоков агента и ссылок на вложения; незакрытый
        блок срезает хвост."""
        tag = AgentTag.SYSTEM_REMINDER
        kept: list[str] = []
        rest = text
        while (start := rest.find(tag.open)) >= 0:
            kept.append(rest[:start])
            end = rest.find(tag.close, start)
            if end < 0:
                rest = ""
                break

            rest = rest[end + len(tag.close) :]

        kept.append(rest)
        return self.LINK.sub("", "".join(kept))


class ReplyBook:
    """Книга ответов стенда: точный текст вопроса → ответ.

    Тест кладёт книгу целиком (`PUT /replies`) и пишет в чат обычный вопрос;
    FakeLlmApp ищет вопрос запроса здесь точно, без нечёткого совпадения. Ход
    книги превращается в сценарий тем же путём, что и маркер `scenario:call`:
    id вызова — хеш вопроса и номер, имя инструмента подставляют
    предложенные модели (OfferedTools).
    """

    NEAREST: ClassVar[int] = 3
    DIGEST_CHARS: ClassVar[int] = 8

    def __init__(self, questions: ReplyQuestions) -> None:
        self._questions = questions
        self._replies: dict[str, Reply] = {}

    def replace(self, replies: Sequence[Reply]) -> None:
        """Ошибки:
        ReplyConflictError — один вопрос пришёл с разными ответами.
        """
        book: dict[str, Reply] = {}
        for reply in replies:
            key = self._questions.normal(reply.question)
            known = book.get(key)
            if known is not None and self._body(known) != self._body(reply):
                msg = (
                    f"fake llm: question {key!r} comes to the book twice with "
                    f"different replies: {self._body(known)} and {self._body(reply)}"
                )
                raise ReplyConflictError(msg)

            book[key] = reply

        self._replies = book

    def has(self, question: str) -> bool:
        return question in self._replies

    def reply(self, question: str) -> Reply:
        return self._replies[question]

    def turn(self, question: str, index: int) -> ReplyTurn:
        """Ход ответа по номеру; после последнего повторяется последний."""
        turns = self._replies[question].turns
        last = len(turns) - 1
        if index > last:
            return turns[last]

        return turns[index]

    def scenario(self, question: str) -> Scenario:
        digest = hashlib.sha256(question.encode("utf-8")).hexdigest()
        stem = f"call_{digest[: self.DIGEST_CHARS]}"
        turns: list[TurnScript] = []
        for number, turn in enumerate(self._replies[question].turns):
            calls: list[ToolCallSpec] = []
            for index, call in enumerate(turn.calls):
                arguments = dict(call.arguments)
                if call.intent is not None:
                    arguments[ToolCallSpec.INTENT_FIELD] = call.intent

                calls.append(
                    ToolCallSpec(
                        call_id=f"{stem}_{number}_{index}",
                        name=call.tool,
                        arguments=json.dumps(arguments, ensure_ascii=False),
                        unsigned=call.intent is None,
                    )
                )

            turns.append(
                TurnScript(
                    reasoning=turn.reasoning, content=turn.content, tool_calls=calls
                )
            )

        return Scenario(turns=turns)

    def missing(self, question: str) -> str:
        """Текст отказа на вопрос, которого нет в книге и у которого нет маркера."""
        nearest = self._nearest(question)
        return (
            f"fake llm: no reply for question {question!r}; the book has "
            f"{len(self._replies)} questions, the nearest are {nearest}"
        )

    def _nearest(self, question: str) -> list[str]:
        """Ближайшие по общему началу: только для текста отказа."""

        def shared(known: str) -> int:
            return len(os.path.commonprefix([known, question]))

        ranked = sorted(self._replies, key=shared, reverse=True)
        return ranked[: self.NEAREST]

    @staticmethod
    def _body(reply: Reply) -> str:
        return json.dumps(
            reply.model_dump(mode="json", exclude={"question"}), ensure_ascii=False
        )


class ReplyHolds:
    """Паузы потоков книги: какой вопрос держится и его отпускание.

    Отпущенный вопрос дальше не держится: отпускание до паузы снимает её
    заранее, без гонки с потоком. Новая книга сбрасывает отпускания.
    """

    def __init__(self) -> None:
        self._released: dict[str, asyncio.Event] = {}
        self._held: set[str] = set()

    def reset(self) -> None:
        for event in self._released.values():
            event.set()

        self._released = {}

    def release(self, question: str) -> None:
        self._event(question).set()

    def held(self) -> list[str]:
        return sorted(self._held)

    async def wait(self, question: str) -> None:
        event = self._event(question)
        if event.is_set():
            return

        self._held.add(question)
        try:
            await event.wait()
        finally:
            self._held.discard(question)

    def _event(self, question: str) -> asyncio.Event:
        event = self._released.get(question)
        if event is None:
            event = asyncio.Event()
            self._released[question] = event

        return event


@dataclass(frozen=True)
class StreamPace:
    """Темп потока ответа: задержка между кадрами и пауза хода книги.

    Пауза встаёт перед кадром номер hold_at: кадры потока начинаются
    токенами рассуждения и текста, поэтому до паузы уходит ровно hold_at
    токенов.
    """

    delay_sec: float
    hold: bool
    hold_at: int
    question: str
    holds: ReplyHolds
    argument_tokens: bool = False
    """Аргументы вызовов уходят кусками-токенами по кадру, как у настоящей
    модели; только протокол OpenAI — ollama шлёт вызов целым объектом."""

    async def before(self, index: int) -> None:
        if not self.hold:
            return

        if index != self.hold_at:
            return

        await self.holds.wait(self.question)


class ReplyBookClient:
    """Книга ответов со стороны теста: кладёт книгу, ждёт паузы потока
    вопроса и отпускает её. base_url — адрес запущенного FakeLlmApp."""

    TIMEOUT_SEC: ClassVar[float] = 10.0
    POLL_SEC: ClassVar[float] = 0.05

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url

    def register(self, replies: Sequence[Reply]) -> None:
        url = f"{self._base_url}{FakeRoute.REPLIES.value}"
        body = ReplyList(replies).model_dump(mode="json")
        response = httpx.put(url, json=body, timeout=self.TIMEOUT_SEC)
        self._expect_ok(f"PUT {url}", response)

    def release(self, question: str) -> None:
        url = f"{self._base_url}{FakeRoute.RELEASE.value}"
        body = ReplyRelease(question=question).model_dump(mode="json")
        response = httpx.post(url, json=body, timeout=self.TIMEOUT_SEC)
        self._expect_ok(f"POST {url}", response)

    def held(self) -> Sequence[str]:
        url = f"{self._base_url}{FakeRoute.HELD.value}"
        response = httpx.get(url, timeout=self.TIMEOUT_SEC)
        self._expect_ok(f"GET {url}", response)
        return ReplyHeld.model_validate(response.json()).held

    def await_held(self, question: str, timeout_sec: float) -> None:
        """Ждёт, пока поток вопроса встанет на паузу."""
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            if question in self.held():
                return

            time.sleep(self.POLL_SEC)

        msg = (
            f"fake llm: the stream of question {question!r} is not held within "
            f"{timeout_sec}s, held are {list(self.held())}"
        )
        raise ScenarioError(msg)

    @staticmethod
    def _expect_ok(action: str, response: httpx.Response) -> None:
        if response.status_code == httpx.codes.OK:
            return

        got = f"{response.status_code}: {response.text[:500]}"
        msg = f"{action}: expected 200, got {got}"
        raise ScenarioError(msg)


@dataclass
class FakeLlmApp:
    """ASGI-приложение провайдера: ответы книги по тексту вопроса, сценарии
    и отказы по маркеру, журнал принятых запросов обоих протоколов."""

    token_delay_sec: float = 0.02
    model: str = "fake-model"
    hang_sec: float = 3600.0
    estimate_usage: bool = False
    """Учёт токенов по знакам запроса и ответа вместо постоянных чисел."""

    unmarked: ScenarioName | None = None
    """Сценарий для сообщения без маркера; None — такое сообщение отвергается."""

    stream_arguments: bool = False
    """Аргументы вызовов сценариев идут кусками-токенами по кадру (OpenAI)."""

    requests: list[dict[str, Any]] = field(default_factory=list)
    _openai: OpenAiWire = field(init=False, repr=False)
    _ollama: OllamaWire = field(init=False, repr=False)
    _failures: FailureBook = field(init=False, repr=False)
    _summaries: SummaryBook = field(init=False, repr=False)
    _usage: UsageBook = field(init=False, repr=False)
    _names: McpToolNames = field(init=False, repr=False)
    _questions: ReplyQuestions = field(init=False, repr=False)
    _book: ReplyBook = field(init=False, repr=False)
    _holds: ReplyHolds = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._names = McpToolNames()
        self._openai = OpenAiWire(self.model)
        self._ollama = OllamaWire(self.model)
        self._failures = FailureBook()
        self._summaries = SummaryBook()
        self._usage = UsageBook(self.estimate_usage)
        self._questions = ReplyQuestions()
        self._book = ReplyBook(self._questions)
        self._holds = ReplyHolds()

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

        self._reply_routes(app)

        @app.post(FakeRoute.COMPLETIONS.value)
        async def completions(request: Request) -> Response:
            return await self._play(request, self._openai)

        @app.post(FakeRoute.OLLAMA_CHAT.value)
        async def ollama_chat(request: Request) -> Response:
            return await self._play(request, self._ollama)

        return app

    def _reply_routes(self, app: FastAPI) -> None:
        """Маршруты книги ответов: книга целиком, отпускание паузы, паузы."""

        @app.put(FakeRoute.REPLIES.value)
        async def replies(request: Request) -> Response:
            """Книга ответов целиком: прежняя книга и её отпускания сбрасываются."""
            return self._replace_book(await request.json())

        @app.post(FakeRoute.RELEASE.value)
        async def release(request: Request) -> Response:
            """Отпускает поток хода книги, стоящий на паузе (hold)."""
            return self._release(await request.json())

        @app.get(FakeRoute.HELD.value)
        async def held() -> JSONResponse:
            """Вопросы, чьи потоки сейчас стоят на паузе."""
            body = ReplyHeld(held=self._holds.held()).model_dump(mode="json")
            return JSONResponse(body)

    def _replace_book(self, raw: object) -> Response:
        try:
            replies = ReplyList.model_validate(raw).root
        except ValidationError as exc:
            route = FakeRoute.REPLIES.value
            msg = f"fake llm: PUT {route} expects a list of replies: {exc}"
            return JSONResponse(
                {"detail": msg}, status_code=httpx.codes.UNPROCESSABLE_ENTITY
            )

        try:
            self._book.replace(replies)
        except ReplyConflictError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=httpx.codes.CONFLICT)

        self._holds.reset()
        return JSONResponse({"status": "ok", "questions": len(replies)})

    def _release(self, raw: object) -> Response:
        try:
            question = self._questions.normal(ReplyRelease.model_validate(raw).question)
        except ValidationError as exc:
            msg = f"fake llm: POST {FakeRoute.RELEASE.value} expects a question: {exc}"
            return JSONResponse(
                {"detail": msg}, status_code=httpx.codes.UNPROCESSABLE_ENTITY
            )

        if not self._book.has(question):
            msg = f"fake llm: release of question {question!r} that is not in the book"
            return JSONResponse({"detail": msg}, status_code=httpx.codes.NOT_FOUND)

        self._holds.release(question)
        return JSONResponse({"status": "ok"})

    async def _play(self, request: Request, wire: OpenAiWire | OllamaWire) -> Response:
        """Общий ход обоих протоколов: ответ книги по вопросу, иначе сценарий
        и отказ по маркеру последнего сообщения пользователя; ответ — в
        формате протокола."""
        payload = await request.json()
        self.requests.append(payload)

        if self._summaries.is_summary_request(payload):
            return await self._play_summary(payload, wire)

        question = self._questions.of(payload)
        if self._book.has(question):
            return await self._play_reply(payload, wire, question)

        text = self._last_user_text(payload)
        failure = FailureName.find(text)
        if self._unanswered(text, failure):
            return wire.rejected(self._book.missing(question))

        played = self._played(payload, text, failure)
        if failure.answers_with_status:
            return wire.error(failure)

        if failure is FailureName.HANG:
            await asyncio.sleep(self.hang_sec)

        pace = StreamPace(
            delay_sec=self.token_delay_sec,
            hold=False,
            hold_at=0,
            question=question,
            holds=self._holds,
            argument_tokens=self.stream_arguments,
        )
        return self._answer(wire, payload, played, pace)

    async def _play_reply(
        self, payload: Mapping[str, Any], wire: OpenAiWire | OllamaWire, question: str
    ) -> Response:
        """Ход книги: номер хода — по вызовам, сыгранным после вопроса."""
        offered = OfferedTools(payload, self._names)
        scenario = offered.scenario(self._book.scenario(question))
        index = scenario.answered(self._called_names(payload))
        turn = self._book.turn(question, index)
        script = scenario.turn(index)
        played = offered.played(self._failures.apply(script, turn.failure))

        if turn.failure.answers_with_status:
            return wire.error(turn.failure)

        if turn.failure is FailureName.HANG:
            await asyncio.sleep(self.hang_sec)

        tokens = self._tokens(played.script)
        hold_at = min(turn.hold_after_tokens, tokens)

        reply = self._book.reply(question)
        pace = StreamPace(
            delay_sec=reply.token_delay_sec,
            hold=turn.hold,
            hold_at=hold_at,
            question=question,
            holds=self._holds,
            argument_tokens=reply.stream_arguments,
        )
        return self._answer(wire, payload, played, pace)

    @staticmethod
    def _tokens(script: TurnScript) -> int:
        """Сколько кадров-токенов идёт в потоке до вызовов и финального кадра."""
        split = TokenSplit()
        reasoning = len(list(split.tokens(script.reasoning)))
        content = len(list(split.tokens(script.content)))
        return reasoning + content

    def _unanswered(self, text: str, failure: FailureName) -> bool:
        """Вопрос без ответа книги, без маркеров и без назначенного сценария."""
        if self.unmarked is not None:
            return False

        if failure is not FailureName.NONE:
            return False

        return ScenarioName.find(text) is None

    async def _play_summary(
        self, payload: Mapping[str, Any], wire: OpenAiWire | OllamaWire
    ) -> Response:
        """Запрос пересказа агента: ответ по маркеру `summary:*`."""
        name = self._summaries.name(payload)
        if name is SummaryName.OVERFLOW:
            return wire.error(FailureName.OVERFLOW)

        if name is SummaryName.HANG:
            await asyncio.sleep(self.hang_sec)

        played = self._summaries.played(payload, name)
        offered = OfferedTools(payload, self._names)
        pace = StreamPace(
            delay_sec=self.token_delay_sec,
            hold=False,
            hold_at=0,
            question="",
            holds=self._holds,
        )

        return self._answer(wire, payload, offered.played(played), pace)

    def _answer(
        self,
        wire: OpenAiWire | OllamaWire,
        payload: Mapping[str, Any],
        played: Played,
        pace: StreamPace,
    ) -> Response:
        usage = self._usage.of(payload, played.script)
        if not payload.get("stream"):
            return wire.completion(played, usage)

        return wire.stream(played, usage, pace)

    def _played(
        self, payload: Mapping[str, Any], text: str, failure: FailureName
    ) -> Played:
        name = ScenarioName.find(text)
        if name is None:
            name = self._unmarked_scenario(text, failure)

        offered = OfferedTools(payload, self._names)
        scenario = offered.scenario(ScenarioBook.of(name, text))
        script = scenario.turn(scenario.answered(self._called_names(payload)))

        return offered.played(self._failures.apply(script, failure))

    def _unmarked_scenario(self, text: str, failure: FailureName) -> ScenarioName:
        """Сценарий сообщения без маркера: назначенный `unmarked`, при одном
        маркере отказа — ответ; без того и другого — ошибка стенда."""
        if self.unmarked is not None:
            return self.unmarked

        if failure is not FailureName.NONE:
            return ScenarioName.ANSWER

        return ScenarioName.of(text)

    @staticmethod
    def _called_names(payload: Mapping[str, Any]) -> list[str]:
        """Имена инструментов, которые ассистент уже вызвал после последнего
        сообщения пользователя с текстом: по ним сценарий узнаёт, какой ход
        отвечать. Сообщение пользователя из одних результатов инструментов
        (ollama не различает роли tool и user по месту) разговор не режет."""
        called: list[str] = []
        messages = payload.get("messages")
        if not messages:
            return called

        for message in reversed(messages):
            role = message.get("role")
            if role == "user" and isinstance(message.get("content"), str):
                return called

            if role != "assistant":
                continue

            if calls := message.get("tool_calls"):
                for call in calls:
                    function = call.get("function") or {}
                    called.append(str(function.get("name")))

        return called

    @staticmethod
    def _last_user_text(payload: Mapping[str, Any]) -> str:
        """Строка с маркером: сообщения пользователя с конца, строки каждого
        с конца; первая строка с маркером сценария или отказа решает.

        Так служебные сообщения агента без маркеров (подсказка после обрыва по
        длине) и слитые сообщения пользователя (неотвеченный промпт прошлого
        хода перед новым) не меняют сценарий текущего запроса.
        """
        messages = payload.get("messages")
        if not messages:
            keys = sorted(payload)
            msg = (
                f"fake llm request: expected a non-empty 'messages' list, "
                f"got keys {keys}"
            )
            raise ScenarioError(msg)

        texts: list[str] = []
        for message in reversed(messages):
            if message.get("role") != "user":
                continue

            content = message.get("content")
            if not isinstance(content, str):
                continue

            texts.append(content)
            for line in reversed(content.splitlines()):
                marked = ScenarioName.find(line) is not None
                failed = FailureName.find(line) is not FailureName.NONE
                if marked or failed:
                    return line

        if texts:
            return texts[0]

        roles = [message.get("role") for message in messages]
        msg = (
            f"fake llm request: no user message with string content among roles {roles}"
        )
        raise ScenarioError(msg)


def serve(host: str, port: int, token_delay_sec: float) -> None:
    """Запуск провайдера отдельным процессом."""
    app = FakeLlmApp(token_delay_sec=token_delay_sec)
    uvicorn.run(app.asgi(), host=host, port=port, log_level="warning")
