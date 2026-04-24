"""Перехват tool call, пришедшего JSON-текстом в поле, отличное от
``tool_calls``. Покрывает два канала (``content`` и ``reasoning_content``)
одним алгоритмом: первый токен с префиксом ``{`` → буферизация → парсинг
на ``GenerationDone``. Нарушения формата уходят в LLM критикой через
:class:`AgentErrorRouter`.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import Enum
from typing import ClassVar

from boba.domain.core.patterns import Converter, StreamSource
from boba_2.domain.agent.errors import LLMToolCallFormatError
from boba_2.domain.agent.events import (
    AgentEvent,
    AnswerDiscarded,
    AnswerStarted,
    AnswerToken,
    GenerationDone,
    ThinkingStarted,
    ThinkingToken,
    ToolCallArgumentDelta,
    ToolCallBegin,
)
from boba_2.domain.agent.models import AgentContext
from boba_2.domain.llm.events import FinishReason
from boba_2.domain.llm.models import RequestId


@dataclass(frozen=True)
class ParsedJsonToolCall:
    name: str
    # Строка, а не dict: контракт события ``ToolCallArgumentDelta`` —
    # стримовое накопление чанков, dict только здесь ломал бы однородность.
    arguments: str


class StrictJsonToolCallParser(Converter[str, ParsedJsonToolCall]):
    """Корневой JSON-объект с ровно двумя полями ``name`` (непустая строка)
    и ``arguments`` (объект/массив). Любое отклонение —
    :class:`LLMToolCallFormatError`.
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


class ChannelMode(Enum):
    UNDECIDED = "undecided"
    BUFFERING = "buffering"
    PASSTHROUGH = "passthrough"

    def is_undecided(self) -> bool:
        return self is ChannelMode.UNDECIDED

    def is_buffering(self) -> bool:
        return self is ChannelMode.BUFFERING

    def is_passthrough(self) -> bool:
        return self is ChannelMode.PASSTHROUGH


_StartedEvent = AnswerStarted | ThinkingStarted
_TokenEvent = AnswerToken | ThinkingToken


@dataclass
class _ChannelState:
    token_cls: type[_TokenEvent]
    started_cls: type[_StartedEvent]
    # content персистится как assistant-message и требует discard при
    # переинтерпретации; thinking в assistant-message не попадает.
    emit_discard: bool
    mode: ChannelMode = ChannelMode.UNDECIDED
    pending_started: _StartedEvent | None = None
    buffer: str = ""

    def owns(self, event: AgentEvent) -> bool:
        return isinstance(event, (self.token_cls, self.started_cls))

    def reset(self) -> None:
        self.mode = ChannelMode.UNDECIDED
        self.pending_started = None
        self.buffer = ""


class StrictJsonContentToolCallMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Срабатывает на ``AnswerToken``/``ThinkingToken`` с префиксом ``{``:
    буферизует канал до ``GenerationDone``, парсит как tool call, переписывает
    поток на ``ToolCall*``-события. Native ``ToolCallBegin`` отменяет оба
    канала. При одновременном buffering-е приоритет у content.

    Ставится **innermost** — внутри
    :class:`~boba_2.domain.agent.meat.dialogue.\
AssistantMessagePersistenceMiddleware`.
    """

    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
    ) -> None:
        self._inner = inner
        self._parser = StrictJsonToolCallParser()

    def name(self) -> str:
        return "StrictJsonContentToolCall"

    def reset(self) -> None:
        self._inner.reset()

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        rid = ctx.agent_request.request_id
        content = _ChannelState(
            token_cls=AnswerToken,
            started_cls=AnswerStarted,
            emit_discard=True,
        )
        reasoning = _ChannelState(
            token_cls=ThinkingToken,
            started_cls=ThinkingStarted,
            emit_discard=False,
        )
        channels = (content, reasoning)

        for event in self._inner.stream(ctx):
            if isinstance(event, ToolCallBegin):
                for ch in channels:
                    ch.pending_started = None
                    if ch.mode.is_undecided():
                        ch.mode = ChannelMode.PASSTHROUGH
                yield event
                continue

            if isinstance(event, GenerationDone):
                yield from self._on_generation_done(rid, event, channels)
                for ch in channels:
                    ch.reset()
                continue

            handled = False
            for ch in channels:
                if ch.owns(event):
                    yield from self._process_channel(ch, event)
                    handled = True
                    break
            if not handled:
                yield event

    def _process_channel(
        self, ch: _ChannelState, event: AgentEvent,
    ) -> Iterator[AgentEvent]:
        if ch.mode.is_passthrough():
            yield event
            return
        if ch.mode.is_buffering():
            if isinstance(event, ch.token_cls):
                ch.buffer += event.token
            yield event
            return
        yield from self._decide(ch, event)

    def _decide(
        self, ch: _ChannelState, event: AgentEvent,
    ) -> Iterator[AgentEvent]:
        if isinstance(event, ch.started_cls):
            ch.pending_started = event
            return
        if isinstance(event, ch.token_cls):
            if ch.pending_started is not None:
                yield ch.pending_started
                ch.pending_started = None
            yield event
            if event.token.lstrip().startswith("{"):
                ch.buffer = event.token
                ch.mode = ChannelMode.BUFFERING
            else:
                ch.mode = ChannelMode.PASSTHROUGH
            return
        yield event

    def _on_generation_done(
        self,
        rid: RequestId,
        event: GenerationDone,
        channels: tuple[_ChannelState, ...],
    ) -> Iterator[AgentEvent]:
        for ch in channels:
            if ch.pending_started is not None:
                yield ch.pending_started
                ch.pending_started = None

        for ch in channels:
            if ch.mode.is_buffering():
                yield from self._finalize(rid, ch.buffer, ch.emit_discard)
                return

        yield event

    def _finalize(
        self, rid: RequestId, raw: str, emit_discard: bool,
    ) -> Iterator[AgentEvent]:
        # При сбое парсинга эмитим GenerationDone(tool_calls) до raise:
        # AssistantMessagePersistenceMiddleware успеет коммитнуть raw буфер,
        # а tool_calls не даёт StopOnFinished оборвать цикл — LLM на
        # следующей итерации увидит свой вывод и критику роутера.
        try:
            parsed = self._parser.convert(raw)
        except LLMToolCallFormatError:
            yield GenerationDone(
                request_id=rid, finish_reason=FinishReason.TOOL_CALLS,
            )
            raise
        if emit_discard:
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
        yield GenerationDone(
            request_id=rid, finish_reason=FinishReason.TOOL_CALLS,
        )
