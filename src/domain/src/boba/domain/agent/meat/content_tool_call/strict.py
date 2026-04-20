"""Строгий вариант: буферизует весь ``content`` до конца генерации,
затем разбирает его как tool call с жёстким whitelist полей.

В отличие от потокового :mod:`.streaming`, не даёт LLM смешивать текст
и JSON — если ответ начался с ``{``, значит это обязан быть чистый tool
call без посторонних полей. Нарушения формата едут через
:class:`AgentErrorRouter` обратно в LLM отдельным ``role="user"``
сообщением с критикой.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import ClassVar

from boba.domain.agent.errors import LLMToolCallFormatError
from boba.domain.agent.events import (
    AgentEvent,
    AnswerDiscarded,
    AnswerStarted,
    AnswerToken,
    FinishReason,
    GenerationDone,
    ToolCallArgumentDelta,
    ToolCallBegin,
)
from boba.domain.agent.models import AgentContext, RequestId
from boba.domain.core.patterns import Converter, StreamSource


@dataclass(frozen=True)
class ParsedJsonToolCall:
    """Распарсенный content-as-JSON tool call.

    ``arguments`` — уже JSON-строка корневого объекта аргументов, готовая
    к эмиту в :class:`ToolCallArgumentDelta` (ровно в том виде, в каком её
    ждёт :class:`ToolExecutionMiddleware._run_tool`, вызывая ``json.loads``
    на входе). Строка, а не ``dict``, потому что контракт событийной
    модели — стримовое накопление чанков аргументов, и точечное введение
    ``dict`` только для content-as-JSON ломало бы однородность.
    """

    name: str
    arguments: str


class StrictJsonToolCallParser(Converter[str, ParsedJsonToolCall]):
    """Строгий парсер content-as-JSON в ``(name, arguments)``.

    Контракт:

    - Корневой JSON — объект с **ровно** двумя полями ``name``, ``arguments``.
    - ``name`` — непустая строка.
    - ``arguments`` — объект или массив.
    - Любое отклонение → :class:`LLMToolCallFormatError` с описанием,
      что именно не так (невалидный JSON, не-объект на корне,
      отсутствующие/посторонние поля, неверные типы). Ошибка не
      терминальна: роутер отдаст её обратно LLM отдельным сообщением,
      LLM переформулирует tool call на следующей итерации.

    Посторонние поля запрещены явно: содержательные ответы пользователю
    нельзя протаскивать внутри tool call — это должен быть чистый сигнал
    диспетчеру, а текст уходит отдельным сообщением.

    Чистая функция от строки — тестируется в изоляции без моков стрима.
    """

    _ALLOWED: ClassVar[frozenset[str]] = frozenset({"name", "arguments"})

    def convert(self, value: str) -> ParsedJsonToolCall:
        try:
            data = json.loads(value)
        except json.JSONDecodeError as e:
            raise LLMToolCallFormatError(
                f"Ответ модели начался с '{{', значит обязан быть "
                f"корректным JSON tool call, но JSON невалиден: "
                f"{e.msg} (позиция {e.pos}). Полученный content: {value!r}",
                raw_content=value,
            ) from e

        if not isinstance(data, dict):
            raise LLMToolCallFormatError(
                f"Ожидался JSON-объект tool call вида "
                f"{{'name': str, 'arguments': object}}, получено значение "
                f"типа {type(data).__name__}: {value!r}",
                raw_content=value,
            )

        missing = sorted(self._ALLOWED - data.keys())
        if missing:
            raise LLMToolCallFormatError(
                f"В JSON tool call отсутствуют обязательные поля: "
                f"{missing}. Ожидался формат "
                f"{{'name': str, 'arguments': object}}. "
                f"Полученный content: {value!r}",
                raw_content=value,
            )

        extra = sorted(data.keys() - self._ALLOWED)
        if extra:
            raise LLMToolCallFormatError(
                f"В JSON tool call присутствуют посторонние поля: "
                f"{extra}. Допустимы только {sorted(self._ALLOWED)}. "
                f"Любые дополнительные поля (например 'content', 'response', "
                f"'type', 'id', 'thought') запрещены — текст для пользователя "
                f"нельзя передавать внутри tool call. "
                f"Полученный content: {value!r}",
                raw_content=value,
            )

        name = data["name"]
        if not isinstance(name, str) or not name.strip():
            raise LLMToolCallFormatError(
                f"Поле 'name' должно быть непустой строкой, получено "
                f"{type(name).__name__}={name!r}. Полученный content: {value!r}",
                raw_content=value,
            )

        arguments = data["arguments"]
        if not isinstance(arguments, (dict, list)):
            raise LLMToolCallFormatError(
                f"Поле 'arguments' (tool '{name}') должно быть объектом или "
                f"массивом, получено {type(arguments).__name__}={arguments!r}. "
                f"Полученный content: {value!r}",
                raw_content=value,
            )

        return ParsedJsonToolCall(
            name=name,
            arguments=json.dumps(arguments, ensure_ascii=False),
        )


class StrictJsonContentToolCallMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Обнаруживает content-as-JSON tool call и оркеструет события.

    Если первый непустой ``AnswerToken`` начинается с ``{`` — весь
    дальнейший ``content`` трактуется как tool call и отдаётся парсеру:
    никакого смешения текста и JSON, ничего кроме корректного вызова
    инструмента.

    Для отзывчивости UI ``AnswerToken``-ы во время буферизации
    **проксируются наружу** — пользователь видит, что модель печатает.
    Параллельно middleware копит их в буфер. На :class:`GenerationDone`:

    - Эмитит :class:`AnswerDiscarded` — сигнал downstream-ам
      (``persistence``, ``history``, sink'и UI) отбросить накопленный
      content: в долговременной истории JSON-текста быть не должно.
    - Отдаёт буфер инъектированному парсеру (``Converter[str,
      ParsedJsonToolCall]``). Парсер сам отвечает за формат JSON,
      допустимые поля, типы и сообщения об ошибках:

      * Успех → :class:`AnswerDiscarded` + :class:`ToolCallBegin` +
        :class:`ToolCallArgumentDelta` + :class:`GenerationDone` с
        ``finish_reason="tool_calls"``.
      * Парсер бросил :class:`LLMToolCallFormatError` — сперва
        эмитим :class:`GenerationDone` с ``finish_reason="tool_calls"``
        (чтобы :class:`AssistantMessagePersistenceMiddleware` успел
        коммитнуть raw content как assistant-сообщение и
        :class:`StopOnFinished` не оборвала цикл: семантика «LLM
        пыталась сделать tool call, но сбойно — продолжаем»), **без**
        :class:`AnswerDiscarded`, после чего исключение летит в
        :class:`AgentErrorRouter`, который добавляет отдельное
        ``role="user"``-сообщение с критикой и эмитит
        :class:`ToolCallFormatFailed`. Цикл не прерывается, LLM
        увидит свой предыдущий вывод + критику на следующей итерации.

    Сама middleware про JSON-синтаксис, whitelist полей и типы ничего
    не знает — только про стриминг событий и границу режимов. Парсер
    заменяется через конструктор (например, мягкий вариант для моделей,
    которые добавляют thinking-поля).

    Если первый ``AnswerToken`` не начинается с ``{`` — поток идёт
    passthrough. Если раньше него приходит настоящий
    :class:`ToolCallBegin` от провайдера — тоже passthrough.

    Ставится innermost — внутри
    :class:`AssistantMessagePersistenceMiddleware`.
    """

    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
        parser: Converter[str, ParsedJsonToolCall] | None = None,
    ) -> None:
        self._inner = inner
        self._parser = parser if parser is not None else StrictJsonToolCallParser()

    def name(self) -> str:
        return "StrictJsonContentToolCall"

    def stream(  # noqa: C901, PLR0912
        self, ctx: AgentContext
    ) -> Iterator[AgentEvent]:
        rid = ctx.request.request_id
        mode = "undecided"
        pending_started: AnswerStarted | None = None
        buffer = ""

        for event in self._inner.stream(ctx):
            if mode == "passthrough":
                yield event
                continue

            if mode == "buffering":
                match event:
                    case AnswerToken(token=t):
                        buffer += t
                        yield event
                    case GenerationDone():
                        yield from self._finalize_tool_call(rid, buffer)
                        mode = "passthrough"
                        buffer = ""
                    case _:
                        yield event
                continue

            match event:
                case AnswerStarted():
                    pending_started = event
                case AnswerToken(token=t) if t.lstrip().startswith("{"):
                    if pending_started is not None:
                        yield pending_started
                        pending_started = None
                    yield event
                    buffer = t
                    mode = "buffering"
                case AnswerToken():
                    if pending_started is not None:
                        yield pending_started
                        pending_started = None
                    mode = "passthrough"
                    yield event
                case ToolCallBegin():
                    pending_started = None
                    mode = "passthrough"
                    yield event
                case GenerationDone():
                    if pending_started is not None:
                        yield pending_started
                        pending_started = None
                    yield event
                    mode = "passthrough"
                case _:
                    yield event

    def _finalize_tool_call(self, rid: RequestId, raw: str) -> Iterator[AgentEvent]:
        """Завершающий этап: буфер уже проэмичен как ``AnswerToken``-ы.

        Успех: эмитим :class:`AnswerDiscarded` (downstream-ы выкидывают
        накопленный content, вместо него пойдут ``ToolCall*``),
        :class:`ToolCallBegin` + :class:`ToolCallArgumentDelta`,
        :class:`GenerationDone` с ``finish_reason="tool_calls"``.

        Сбой парсера (:class:`LLMToolCallFormatError`): эмитим
        :class:`GenerationDone` с ``finish_reason="tool_calls"`` (без
        ``AnswerDiscarded`` — raw content должен остаться в буфере
        персистенса, чтобы коммитнуться как assistant-сообщение и LLM
        увидела свой предыдущий вывод на следующей итерации; а
        ``tool_calls`` не даёт :class:`StopOnFinished` оборвать цикл —
        семантика «LLM хотела tool call»), после чего пробрасываем
        исключение наверх в :class:`AgentErrorRouter`.
        """
        try:
            parsed = self._parser.convert(raw)
        except LLMToolCallFormatError:
            yield GenerationDone(
                request_id=rid, finish_reason=FinishReason.TOOL_CALLS
            )
            raise
        yield AnswerDiscarded(request_id=rid)
        yield ToolCallBegin(
            request_id=rid,
            index=0,
            tool_call_id=f"call_{parsed.name}",
            tool_name=parsed.name,
        )
        yield ToolCallArgumentDelta(
            request_id=rid,
            index=0,
            arguments=parsed.arguments,
        )
        yield GenerationDone(request_id=rid, finish_reason=FinishReason.TOOL_CALLS)
