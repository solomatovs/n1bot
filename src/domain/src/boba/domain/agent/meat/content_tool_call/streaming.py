"""Потоковая эвристика: детектит tool call в ``content`` и
перерабатывает его в ``ToolCall*`` события по мере прихода чанков.

Подходит для моделей, которые «галлюцинируют» tool call JSON-текстом в
``content`` вместо структурированного поля ``tool_calls``. Ключевое
отличие от строгого варианта — :class:`JsonContentToolCallMiddleware`
**не ждёт** конца генерации, чтобы распарсить буфер: как только в
префиксе доступно имя инструмента, сразу эмитит :class:`ToolCallBegin`
и дальше стримит аргументы чанк-в-чанк.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum, auto

from boba.domain.agent.events import (
    AgentEvent,
    AnswerStarted,
    AnswerToken,
    GenerationDone,
    ToolCallArgumentDelta,
    ToolCallBegin,
)
from boba.domain.agent.models import AgentContext, RequestId
from boba.domain.core.patterns import Converter, StreamSource


@dataclass(frozen=True)
class JsonToolCallHeader:
    """Извлечённые из JSON-префикса имя тула и позиция начала ``arguments``."""

    name: str
    args_start_idx: int


class JsonHeaderParser(Converter[str, JsonToolCallHeader | None]):
    """Инкрементальный парсер префикса ``{"name": "...", "arguments": <{|[>...``.

    Возвращает :class:`JsonToolCallHeader` как только в накопленном тексте
    доступно имя тула и позиция начала значения ``arguments`` (объект или
    массив). ``None`` — пока не хватает текста или структура не подходит.
    Чистая функция от строки — удобно дёргать на каждый новый чанк.
    """

    _NAME_RX = re.compile(r'"name"\s*:\s*"((?:[^"\\]|\\.)*)"')
    _ARGS_RX = re.compile(r'"arguments"\s*:\s*')

    def convert(self, value: str) -> JsonToolCallHeader | None:
        if not value.lstrip().startswith("{"):
            return None
        mname = self._NAME_RX.search(value)
        if mname is None:
            return None
        margs = self._ARGS_RX.search(value, mname.end())
        if margs is None:
            return None
        i = margs.end()
        while i < len(value) and value[i] in " \t\n\r":
            i += 1
        if i >= len(value) or value[i] not in "{[":
            return None
        return JsonToolCallHeader(name=mname.group(1), args_start_idx=i)


class JsonDepthScanner:
    """Трекер глубины JSON-структуры от начала скана.

    Хранит состояние между вызовами :meth:`consume`, учитывает строки и
    экранирование — ``{`` / ``}`` / ``[`` / ``]`` внутри строк не влияют
    на глубину. В отличие от парсера значений, это именно счётчик: не
    «стартует» / «заканчивает», а непрерывно отслеживает текущий depth.

    Метод :meth:`consume` умеет «остановиться» когда depth упал до заданного
    ``target_depth`` — полезно для поэтапной обработки: сначала сжать
    значение ``arguments`` до уровня внешней обёртки, затем дочитать обёртку
    до 0, затем всё что после — это уже пост-обёрточный текст.
    """

    def __init__(self) -> None:
        self._depth = 0
        self._in_str = False
        self._escape = False

    @property
    def depth(self) -> int:
        return self._depth

    def consume(
        self, text: str, target_depth: int | None = None
    ) -> tuple[str, str]:
        """Обрабатывает ``text``, обновляя глубину.

        Если ``target_depth`` задан, останавливается на символе, который
        опустил глубину до ``target_depth`` (включительно). Возвращает
        ``(consumed, remaining)``: ``consumed`` включает этот закрывающий
        символ, ``remaining`` — хвост после него.

        Если ``target_depth=None``, потребляет весь ``text`` и возвращает
        ``(text, "")``.
        """
        for i, ch in enumerate(text):
            if self._in_str:
                if self._escape:
                    self._escape = False
                elif ch == "\\":
                    self._escape = True
                elif ch == '"':
                    self._in_str = False
                continue
            if ch == '"':
                self._in_str = True
                continue
            if ch in "{[":
                self._depth += 1
                continue
            if ch in "}]":
                self._depth -= 1
                if target_depth is not None and self._depth <= target_depth:
                    return text[: i + 1], text[i + 1 :]
        return text, ""


class _ParserState(Enum):
    UNDECIDED = auto()
    PARSE_HEADER = auto()
    STREAM_ARGS = auto()
    CONSUME_WRAPPER = auto()
    TAIL = auto()
    PASSTHROUGH = auto()


class JsonContentToolCallMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Эвристика: маленькие модели (Qwen3:8b/Ollama и т.п.) часто
    «галлюцинируют» tool call как JSON-текст в ``content`` вместо
    структурированного поля ``tool_calls``. Этот middleware смотрит на
    **первый** непустой ``AnswerToken``: если он начинается с ``{`` —
    стримит текст в режиме парсера ``{"name": ..., "arguments": {...}}``,
    эмитит :class:`ToolCallBegin` как только извлёк ``name``, дальше
    отдаёт содержимое ``arguments`` как :class:`ToolCallArgumentDelta` по
    мере прихода чанков, подменяет ``finish_reason`` на ``"tool_calls"``.

    Работает потоково — не накапливает полный ответ, :class:`ToolCallBegin`
    уходит наружу как только доступно имя (обычно после 20–50 байт).
    Если JSON не распарсится (нет ключей ``name``/``arguments``, невалидный
    синтаксис) — буфер отдаётся как обычный ``AnswerToken`` без изменений.
    Если параллельно пришёл настоящий :class:`ToolCallBegin` — буфер
    сбрасывается как текст, дальше passthrough.

    Ставится **innermost** — внутри :class:`AssistantMessagePersistenceMiddleware`,
    чтобы тот аккумулировал уже переписанные события и эмитил
    :class:`ToolCallComplete` для downstream-а.

    Зависимости выделены в самостоятельные паттерны:
    :class:`JsonHeaderParser` — ``Converter[str, JsonToolCallHeader | None]``
    для разбора префикса; :class:`JsonDepthScanner` — для потокового трекинга
    границы значения ``arguments``. Оба переопределяются через конструктор,
    облегчая тестирование и замену эвристик.
    """

    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
        parser: Converter[str, JsonToolCallHeader | None] | None = None,
    ) -> None:
        self._inner = inner
        self._parser = parser if parser is not None else JsonHeaderParser()

    def name(self) -> str:
        return "JsonContentToolCall"

    def stream(  # noqa: C901, PLR0912, PLR0915
        self, ctx: AgentContext
    ) -> Iterator[AgentEvent]:
        rid = ctx.request.request_id
        state = _ParserState.UNDECIDED
        pending_started: AnswerStarted | None = None
        header_buffer = ""
        scanner = JsonDepthScanner()
        outer_depth = 0
        tail_answer_started = False

        for event in self._inner.stream(ctx):
            match (state, event):
                case (_ParserState.PASSTHROUGH, _):
                    yield event

                case (_ParserState.TAIL, AnswerToken(token=t)):
                    if not tail_answer_started:
                        tail_answer_started = True
                        yield AnswerStarted(request_id=rid)
                    yield AnswerToken(request_id=rid, token=t)
                case (_ParserState.TAIL, GenerationDone()):
                    yield GenerationDone(
                        request_id=rid, finish_reason="tool_calls"
                    )
                    state = _ParserState.PASSTHROUGH

                case (
                    _ParserState.STREAM_ARGS | _ParserState.CONSUME_WRAPPER,
                    AnswerToken(token=t),
                ):
                    state, args_out, tail_out = self._advance(
                        t, state, scanner, outer_depth
                    )
                    if args_out:
                        yield ToolCallArgumentDelta(
                            request_id=rid, index=0, arguments=args_out
                        )
                    if tail_out:
                        if not tail_answer_started:
                            tail_answer_started = True
                            yield AnswerStarted(request_id=rid)
                        yield AnswerToken(request_id=rid, token=tail_out)
                case (
                    _ParserState.STREAM_ARGS | _ParserState.CONSUME_WRAPPER,
                    GenerationDone(),
                ):
                    yield GenerationDone(
                        request_id=rid, finish_reason="tool_calls"
                    )
                    state = _ParserState.PASSTHROUGH

                case (_ParserState.PARSE_HEADER, AnswerToken(token=t)):
                    header_buffer += t
                    header = self._parser.convert(header_buffer)
                    if header is not None:
                        pending_started = None
                        yield ToolCallBegin(
                            request_id=rid,
                            index=0,
                            tool_call_id=f"call_{header.name}",
                            tool_name=header.name,
                        )
                        scanner.consume(header_buffer[: header.args_start_idx])
                        outer_depth = scanner.depth
                        state = _ParserState.STREAM_ARGS
                        state, args_out, tail_out = self._advance(
                            header_buffer[header.args_start_idx :],
                            state,
                            scanner,
                            outer_depth,
                        )
                        header_buffer = ""
                        if args_out:
                            yield ToolCallArgumentDelta(
                                request_id=rid, index=0, arguments=args_out
                            )
                        if tail_out:
                            tail_answer_started = True
                            yield AnswerStarted(request_id=rid)
                            yield AnswerToken(request_id=rid, token=tail_out)
                case (_ParserState.PARSE_HEADER, GenerationDone()):
                    yield from self._flush_header_as_text(
                        rid, pending_started, header_buffer
                    )
                    pending_started = None
                    header_buffer = ""
                    yield event
                    state = _ParserState.PASSTHROUGH
                case (_ParserState.PARSE_HEADER, ToolCallBegin()):
                    yield from self._flush_header_as_text(
                        rid, pending_started, header_buffer
                    )
                    pending_started = None
                    header_buffer = ""
                    state = _ParserState.PASSTHROUGH
                    yield event

                case (_ParserState.UNDECIDED, AnswerStarted()):
                    pending_started = event
                case (_ParserState.UNDECIDED, AnswerToken(token=t)) if (
                    t.lstrip().startswith("{")
                ):
                    state = _ParserState.PARSE_HEADER
                    header_buffer = t
                    header = self._parser.convert(header_buffer)
                    if header is not None:
                        pending_started = None
                        yield ToolCallBegin(
                            request_id=rid,
                            index=0,
                            tool_call_id=f"call_{header.name}",
                            tool_name=header.name,
                        )
                        scanner.consume(header_buffer[: header.args_start_idx])
                        outer_depth = scanner.depth
                        state = _ParserState.STREAM_ARGS
                        state, args_out, tail_out = self._advance(
                            header_buffer[header.args_start_idx :],
                            state,
                            scanner,
                            outer_depth,
                        )
                        header_buffer = ""
                        if args_out:
                            yield ToolCallArgumentDelta(
                                request_id=rid, index=0, arguments=args_out
                            )
                        if tail_out:
                            tail_answer_started = True
                            yield AnswerStarted(request_id=rid)
                            yield AnswerToken(request_id=rid, token=tail_out)
                case (_ParserState.UNDECIDED, AnswerToken()):
                    if pending_started is not None:
                        yield pending_started
                        pending_started = None
                    state = _ParserState.PASSTHROUGH
                    yield event
                case (_ParserState.UNDECIDED, ToolCallBegin()):
                    pending_started = None
                    state = _ParserState.PASSTHROUGH
                    yield event
                case (_ParserState.UNDECIDED, GenerationDone()):
                    if pending_started is not None:
                        yield pending_started
                        pending_started = None
                    yield event
                    state = _ParserState.PASSTHROUGH

                case _:
                    yield event

    @staticmethod
    def _advance(
        text: str,
        state: _ParserState,
        scanner: JsonDepthScanner,
        outer_depth: int,
    ) -> tuple[_ParserState, str, str]:
        """Каскадом прогоняет ``text`` через состояния
        ``STREAM_ARGS → CONSUME_WRAPPER → TAIL``, возвращает новое состояние и
        куски, которые нужно эмитить: ``args_out`` (в ``arguments``) и
        ``tail_out`` (в ``AnswerToken`` после закрытия обёртки).
        """
        args_out = ""
        tail_out = ""
        remaining = text

        if state == _ParserState.STREAM_ARGS and remaining:
            consumed, remaining = scanner.consume(
                remaining, target_depth=outer_depth
            )
            args_out += consumed
            if scanner.depth <= outer_depth:
                state = _ParserState.CONSUME_WRAPPER

        if state == _ParserState.CONSUME_WRAPPER and remaining:
            _discarded, remaining = scanner.consume(remaining, target_depth=0)
            if scanner.depth <= 0:
                state = _ParserState.TAIL

        if state == _ParserState.TAIL and remaining:
            tail_out = remaining

        return state, args_out, tail_out

    @staticmethod
    def _flush_header_as_text(
        rid: RequestId,
        pending_started: AnswerStarted | None,
        header_buffer: str,
    ) -> Iterator[AgentEvent]:
        if pending_started is not None:
            yield pending_started
        if header_buffer:
            yield AnswerToken(request_id=rid, token=header_buffer)
