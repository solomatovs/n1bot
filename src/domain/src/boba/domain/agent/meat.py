"""Middleware-слои AgentLoop."""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from collections.abc import Iterator

from boba.domain.agent.errors import LLMError, RetryableLLMError
from boba.domain.agent.events import (
    AgentEvent,
    AnswerComplete,
    AnswerStarted,
    AnswerToken,
    GenerationDone,
    GenerationFailed,
    GenerationStarted,
    PersistenceFailed,
    PromptFailed,
    RefusalComplete,
    RefusalToken,
    StageCompleted,
    StageStarted,
    ThinkingComplete,
    ThinkingToken,
    ToolCallArgumentDelta,
    ToolCallBegin,
    ToolCallComplete,
    ToolResultReady,
    UserQueryReceived,
)
from boba.domain.agent.models import (
    AgentConfig,
    AgentContext,
    AgentRequest,
    LLMMessage,
    LLMToolCall,
    RequestId,
)
from boba.domain.core.history import HistoryError, HistoryService
from boba.domain.core.messages import MessageService
from boba.domain.core.patterns import (
    Specification,
    StreamSink,
    StreamSource,
    StreamSourceLoop,
)
from boba.domain.core.promt import (
    PromptError,
    PromptFactory,
    PromptKind,
    PromptProvider,
    RetryablePromptError,
)
from boba.domain.core.tools import (
    ToolCall,
    ToolId,
    ToolResult,
    ToolsService,
)

logger = logging.getLogger(__name__)


class Agent:
    def __init__(
        self,
        source: StreamSourceLoop[AgentContext, AgentEvent],
        sink: StreamSink[AgentContext, AgentEvent],
    ) -> None:
        self._source = source
        self._sink = sink

    def name(self) -> str:
        return "AgentLoop"

    def run(self, config: AgentConfig, request: AgentRequest):
        """
        Запускает цикл обработки запроса агентом.
        """
        ctx = AgentContext(
            request=request,
            config=config,
        )

        for event in self._source.stream(ctx):
            self._sink.handle(ctx, event)


class SystemPromptMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Строит system-prompt через :class:`PromptFactory` (срез
    ``PromptKind.SYSTEM``) и кладёт его в ``ctx.llm_builder.system_prompt``.
    :class:`LLMRequestFactory` читает этот слот при сборке :class:`LLMRequest`.
    Отключение middleware через DI убирает system-prompt из запроса.
    """

    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
        prompt_providers: list[PromptProvider],
    ) -> None:
        self._inner = inner
        self._prompt_providers = prompt_providers

    def name(self) -> str:
        return "SystemPrompt"

    def stream(self, ctx: AgentContext) -> Iterator[AgentEvent]:
        content = (
            PromptFactory(ctx, self._prompt_providers)
            .build()
            .to_string(PromptKind.SYSTEM)
        )
        if content:
            ctx.llm_builder.system_prompt = content

        yield from self._inner.stream(ctx)


class UserPromptMiddleware(StreamSource[AgentContext, AgentEvent]):
    """На первой итерации строит user-prompt через :class:`PromptFactory`
    (срез ``PromptKind.USER``) и добавляет его как
    :class:`LLMMessage` с ``role="user"`` в :class:`MessageService`.
    Эмитит :class:`UserQueryReceived` для sink'ов.

    User-prompt — часть диалога (append-once), а не пересчитываемый
    слот. Его правильное место — в :class:`MessageService`, где он
    становится в конец истории (после replay'а предыдущих сессий) и
    дальше не меняется. :class:`LLMRequestFactory` читает
    :class:`MessageService` и не знает, кто его туда положил.
    """

    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
        prompt_providers: list[PromptProvider],
        message_service: MessageService,
    ) -> None:
        self._inner = inner
        self._prompt_providers = prompt_providers
        self._message_service = message_service

    def name(self) -> str:
        return "UserPrompt"

    def stream(self, ctx: AgentContext) -> Iterator[AgentEvent]:
        if ctx.iteration == 1:
            yield StageStarted(request_id=ctx.request.request_id, stage=self.name())
            yield UserQueryReceived(
                request_id=ctx.request.request_id,
                query=ctx.request.query,
            )

            content = (
                PromptFactory(ctx, self._prompt_providers)
                .build()
                .to_string(PromptKind.USER)
            )
            if content:
                self._message_service.add(LLMMessage(role="user", content=content))

            yield StageCompleted(
                request_id=ctx.request.request_id,
                stage=self.name(),
                detail="user prompt added",
            )

        yield from self._inner.stream(ctx)


class AssistantMessagePersistenceMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Агрегирует стриминговые события LLM в assistant-сообщение и
    коммитит его в :class:`MessageService`.

    Наблюдает события от inner-стрима:

    - ``AnswerToken`` → аккумулирует ``content``;
    - ``ThinkingToken`` / ``RefusalToken`` → аккумулируют свои буферы
      (для сопутствующих ``*Complete`` событий);
    - ``ToolCallBegin`` + ``ToolCallArgumentDelta`` → аккумулируют
      tool_calls;
    - ``GenerationStarted`` → сброс буферов (корректность ретраев
      :class:`StupidRetryLLMMiddleware`);
    - ``GenerationDone`` → flush: эмитит ``*Complete`` события в стрим и
      коммитит ``LLMMessage(role="assistant", content=..., tool_calls=...)``
      в :class:`MessageService`.

    Попутно эмитит агрегированные ``*Complete`` события **в стрим** —
    это делает их доступными downstream middleware'ам (например,
    :class:`ToolExecutionMiddleware` ждёт ``ToolCallComplete``) и
    sink'ам. Без этого middleware в стриме живут только токены, а
    ``*Complete`` рождаются отдельно лишь внутри
    :class:`HistorySink` для журнала.
    """

    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
        message_service: MessageService,
    ) -> None:
        self._inner = inner
        self._message_service = message_service
        self._thinking: dict[RequestId, list[str]] = defaultdict(list)
        self._answer: dict[RequestId, list[str]] = defaultdict(list)
        self._refusal: dict[RequestId, list[str]] = defaultdict(list)
        self._tool_calls: dict[RequestId, dict[int, tuple[str, str, list[str]]]] = (
            defaultdict(dict)
        )

    def name(self) -> str:
        return "AssistantPersistence"

    def stream(self, ctx: AgentContext) -> Iterator[AgentEvent]:
        for event in self._inner.stream(ctx):
            match event:
                case GenerationStarted(request_id=rid):
                    self._reset(rid)
                    yield event
                case ThinkingToken(request_id=rid, token=t):
                    self._thinking[rid].append(t)
                    yield event
                case AnswerToken(request_id=rid, token=t):
                    self._answer[rid].append(t)
                    yield event
                case RefusalToken(request_id=rid, token=t):
                    self._refusal[rid].append(t)
                    yield event
                case ToolCallBegin(
                    request_id=rid, index=i, tool_call_id=tid, tool_name=fn
                ):
                    self._tool_calls[rid][i] = (tid, fn, [])
                    yield event
                case ToolCallArgumentDelta(request_id=rid, index=i, arguments=a):
                    if i in self._tool_calls[rid]:
                        self._tool_calls[rid][i][2].append(a)
                    yield event
                case GenerationDone(request_id=rid):
                    yield from self._flush(rid)
                    yield event
                case _:
                    yield event

    def _reset(self, rid: RequestId) -> None:
        self._thinking.pop(rid, None)
        self._answer.pop(rid, None)
        self._refusal.pop(rid, None)
        self._tool_calls.pop(rid, None)

    def _flush(self, rid: RequestId) -> Iterator[AgentEvent]:
        thinking_parts = self._thinking.pop(rid, [])
        answer_parts = self._answer.pop(rid, [])
        refusal_parts = self._refusal.pop(rid, [])
        tool_calls_raw = self._tool_calls.pop(rid, {})

        if thinking_parts:
            yield ThinkingComplete(request_id=rid, content="".join(thinking_parts))
        if answer_parts:
            yield AnswerComplete(request_id=rid, content="".join(answer_parts))
        if refusal_parts:
            yield RefusalComplete(request_id=rid, content="".join(refusal_parts))

        tool_calls: list[LLMToolCall] = []
        for _, (tid, fn, args_parts) in sorted(tool_calls_raw.items()):
            args_str = "".join(args_parts)
            yield ToolCallComplete(
                request_id=rid,
                tool_call_id=tid,
                tool_name=fn,
                arguments=args_str,
            )
            tool_calls.append(LLMToolCall(id=tid, name=fn, arguments=args_str))

        content = "".join(answer_parts) or "".join(refusal_parts)
        if content or tool_calls:
            self._message_service.add(
                LLMMessage(
                    role="assistant",
                    content=content,
                    tool_calls=tool_calls,
                )
            )


class HistoryReplayMiddleware(StreamSource[AgentContext, AgentEvent]):
    """На первом заходе в стрим реконструирует диалог из
    :class:`HistoryService` и вливает :class:`LLMMessage`-и в
    :class:`MessageService`. На повторных заходах — no-op.

    Читает агрегированные события журнала в хронологическом порядке:

    - ``UserQueryReceived`` → ``LLMMessage(role="user")``;
    - ``AnswerComplete`` + ``ToolCallComplete`` между ними и
      ``GenerationDone`` → ``LLMMessage(role="assistant", content, tool_calls)``
      (один assistant-message на один ``GenerationDone``);
    - ``ToolResultReady`` → ``LLMMessage(role="tool", content, tool_call_id)``.

    Остальные события (токены, ``Stage*``, ``*Started``) игнорируются —
    они уже агрегированы в ``*Complete``.
    """

    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
        history: HistoryService,
        message_service: MessageService,
    ) -> None:
        self._inner = inner
        self._history = history
        self._message_service = message_service
        self._replayed = False

    def name(self) -> str:
        return "HistoryReplay"

    def stream(self, ctx: AgentContext) -> Iterator[AgentEvent]:
        if not self._replayed:
            self._replayed = True
            self._replay()
        yield from self._inner.stream(ctx)

    def _replay(self) -> None:
        answer: dict[RequestId, str] = {}
        tool_calls: dict[RequestId, list[LLMToolCall]] = defaultdict(list)

        for entry in self._history.entries():
            rid = entry.request_id
            event = entry.event
            match event:
                case UserQueryReceived(query=q):
                    self._message_service.add(LLMMessage(role="user", content=q))
                case AnswerComplete(content=c):
                    answer[rid] = c
                case ToolCallComplete(tool_call_id=tid, tool_name=fn, arguments=a):
                    tool_calls[rid].append(LLMToolCall(id=tid, name=fn, arguments=a))
                case GenerationDone():
                    content = answer.pop(rid, "")
                    calls = tool_calls.pop(rid, [])
                    if content or calls:
                        self._message_service.add(
                            LLMMessage(
                                role="assistant",
                                content=content,
                                tool_calls=calls,
                            )
                        )
                case ToolResultReady(tool_call_id=tid, content=c):
                    self._message_service.add(
                        LLMMessage(role="tool", content=c, tool_call_id=tid)
                    )
                case _:
                    pass


class ToolsDefinitionMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Кладёт текущий снимок каталога :class:`ToolsService` в
    ``ctx.llm_builder.tools``. :class:`LLMRequestFactory` читает этот слот
    при сборке :class:`LLMRequest` и мапит в ``kwargs["tools"]`` провайдера.

    Отключение middleware через DI — запрос уходит без tools, LLM не видит
    инструментов и не вызывает их. Плагины могут зарегистрировать свою
    реализацию (фильтрация по ролям, лимит по количеству, динамический
    ``tool_choice``) без правки фабрики.
    """

    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
        tools_service: ToolsService,
    ) -> None:
        self._inner = inner
        self._tools_service = tools_service

    def name(self) -> str:
        return "ToolsDefinition"

    def stream(self, ctx: AgentContext) -> Iterator[AgentEvent]:
        ctx.llm_builder.tools = list(self._tools_service.tools())
        yield from self._inner.stream(ctx)


class ToolExecutionMiddleware(StreamSource[AgentContext, AgentEvent]):
    """
    Выполняет tool calls, полученные от LLM.

    После того как внутренний стрим (LLM) заканчивает итерацию, собирает все
    ``ToolCallComplete`` события и по каждому:

    1. Парсит JSON ``arguments``. Битый JSON → ``ToolResult(is_error=True)``
       с понятным сообщением (LLM получит его обратно и сможет починить).
    2. Вызывает :meth:`ToolsService.execute`. Любые ошибки выполнения сам
       сервис завернёт в ``ToolResult(is_error=True)`` — исключения наружу
       не летят.
    3. Пишет ``LLMMessage(role="tool", tool_call_id=..., content=...)`` в
       :class:`MessageService` — на следующей итерации LLM увидит результат.
    4. Эмитит ``ToolResultReady`` в стрим событий — sink'ы его отрисуют/
       залогируют.

    Если LLM не запросила тулов — middleware просто проксирует события
    inner без побочных эффектов.
    """

    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
        tools_service: ToolsService,
        message_service: MessageService,
    ) -> None:
        self._inner = inner
        self._tools_service = tools_service
        self._message_service = message_service

    def name(self) -> str:
        return "ToolExecution"

    def stream(self, ctx: AgentContext) -> Iterator[AgentEvent]:
        pending: list[ToolCallComplete] = []

        for event in self._inner.stream(ctx):
            yield event
            if isinstance(event, ToolCallComplete):
                pending.append(event)

        for tc in pending:
            yield from self._run_tool(tc)

    def _run_tool(self, tc: ToolCallComplete) -> Iterator[AgentEvent]:
        try:
            arguments = json.loads(tc.arguments)
        except json.JSONDecodeError as e:
            result = ToolResult(
                content=f"invalid JSON arguments: {e}",
                is_error=True,
            )
        else:
            call = ToolCall(
                tool_id=ToolId(tc.tool_name),
                arguments=arguments,
            )
            result = self._tools_service.execute(None, call)

        self._message_service.add(
            LLMMessage(
                role="tool",
                content=result.content,
                tool_call_id=tc.tool_call_id,
            ),
        )

        yield ToolResultReady(
            request_id=tc.request_id,
            tool_call_id=tc.tool_call_id,
            tool_name=tc.tool_name,
            content=result.content,
            is_error=result.is_error,
        )


class IterationCounterMiddleware(StreamSource[AgentContext, AgentEvent]):
    """
    Подсчет кол-ва итераций цикла агента.
    Увеличивает счетчик в контексте и делегирует следующему слою.
    """

    def __init__(self, inner: StreamSource[AgentContext, AgentEvent]) -> None:
        self._inner = inner

    def name(self) -> str:
        return "Counter"

    def stream(self, ctx: AgentContext) -> Iterator[AgentEvent]:
        ctx.iteration += 1
        yield from self._inner.stream(ctx)


class ErrorToEventMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Ловит :class:`LLMError` из внутреннего стрима и преобразует в
    терминальное :class:`GenerationFailed`-событие.

    Ставится **снаружи** :class:`StupidRetryLLMMiddleware`: retry уже
    отработал и либо исчерпал попытки (``RetryableLLMError``), либо сразу
    пробросил ``PermanentLLMError``. Выше по стеку исключений ``LLMError``
    уже не будет — клиенты подписываются только на поток событий.
    """

    def __init__(self, inner: StreamSource[AgentContext, AgentEvent]) -> None:
        self._inner = inner

    def name(self) -> str:
        return "ErrorToEvent"

    def stream(self, ctx: AgentContext) -> Iterator[AgentEvent]:
        try:
            yield from self._inner.stream(ctx)
        except LLMError as e:
            yield GenerationFailed(
                request_id=ctx.request.request_id,
                error_kind=type(e).__name__,
                message=str(e),
                retryable=isinstance(e, RetryableLLMError),
                status_code=e.status_code,
            )


class PromptErrorToEventMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Ловит :class:`PromptError` из провайдеров промптов и превращает в
    терминальное :class:`PromptFailed`-событие.

    Ставится **снаружи** :class:`SystemPromptMiddleware` /
    :class:`UserPromptMiddleware`. Узкий тип (``PromptError``) — программные
    баги провайдеров (``KeyError``, ``AttributeError``) проходят насквозь и
    ломают цикл.
    """

    def __init__(self, inner: StreamSource[AgentContext, AgentEvent]) -> None:
        self._inner = inner

    def name(self) -> str:
        return "PromptErrorToEvent"

    def stream(self, ctx: AgentContext) -> Iterator[AgentEvent]:
        try:
            yield from self._inner.stream(ctx)
        except PromptError as e:
            yield PromptFailed(
                request_id=ctx.request.request_id,
                error_kind=type(e).__name__,
                message=str(e),
                retryable=isinstance(e, RetryablePromptError),
                provider=e.provider,
            )


class PersistenceErrorToEventMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Ловит :class:`HistoryError` из :class:`HistoryService` и превращает в
    терминальное :class:`PersistenceFailed`-событие.

    Покрывает чтение журнала (``HistoryReplayMiddleware``). Запись журнала в
    ``HistorySink`` идёт вне source-чейна и этим middleware не ловится.
    """

    def __init__(self, inner: StreamSource[AgentContext, AgentEvent]) -> None:
        self._inner = inner

    def name(self) -> str:
        return "PersistenceErrorToEvent"

    def stream(self, ctx: AgentContext) -> Iterator[AgentEvent]:
        try:
            yield from self._inner.stream(ctx)
        except HistoryError as e:
            yield PersistenceFailed(
                request_id=ctx.request.request_id,
                error_kind=type(e).__name__,
                message=str(e),
                retryable=False,
            )


class JsonContentToolCallMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Эвристика: маленькие модели (Qwen3:8b/Ollama и т.п.) часто
    «галлюцинируют» tool call как JSON-текст в ``content`` вместо
    структурированного поля ``tool_calls``. Этот middleware смотрит на
    **первый** непустой ``AnswerToken``: если он начинается с ``{`` —
    стримит текст в режиме парсера ``{"name": ..., "arguments": {...}}``,
    эмитит :class:`ToolCallBegin` как только извлёк ``name``, дальше
    отдаёт содержимое ``arguments`` как :class:`ToolCallArgumentDelta` по
    мере прихода чанков, подменяет ``finish_reason`` на ``"tool_calls"``.

    Работает потоково — не накапливает полный ответ, `ToolCallBegin`
    уходит наружу как только доступно имя (обычно после 20–50 байт).
    Если JSON не распарсится (нет ключей ``name``/``arguments``, невалидный
    синтаксис) — буфер отдаётся как обычный ``AnswerToken`` без изменений.
    Если параллельно пришёл настоящий :class:`ToolCallBegin` — буфер
    сбрасывается как текст, дальше passthrough.

    Ставится **innermost** — внутри :class:`AssistantMessagePersistenceMiddleware`,
    чтобы тот аккумулировал уже переписанные события и эмитил
    :class:`ToolCallComplete` для downstream-а.
    """

    _NAME_RX = re.compile(r'"name"\s*:\s*"((?:[^"\\]|\\.)*)"')
    _ARGS_RX = re.compile(r'"arguments"\s*:\s*')

    _UNDECIDED = "undecided"
    _PARSE_HEADER = "parse_header"
    _STREAM_ARGS = "stream_args"
    _TAIL = "tail"
    _PASSTHROUGH = "passthrough"

    def __init__(self, inner: StreamSource[AgentContext, AgentEvent]) -> None:
        self._inner = inner

    def name(self) -> str:
        return "JsonContentToolCall"

    def stream(
        self, ctx: AgentContext
    ) -> Iterator[AgentEvent]:
        rid = ctx.request.request_id
        state = self._UNDECIDED
        pending_started: AnswerStarted | None = None
        header_buffer = ""
        depth = 0

        for event in self._inner.stream(ctx):
            if state == self._PASSTHROUGH:
                yield event
                continue

            if state == self._TAIL:
                if isinstance(event, AnswerToken):
                    continue
                if isinstance(event, GenerationDone):
                    yield GenerationDone(
                        request_id=rid, finish_reason="tool_calls"
                    )
                    state = self._PASSTHROUGH
                    continue
                yield event
                continue

            if state == self._STREAM_ARGS:
                if isinstance(event, AnswerToken):
                    consumed, depth, ended = self._scan_args_delta(
                        event.token, depth
                    )
                    if consumed:
                        yield ToolCallArgumentDelta(
                            request_id=rid, index=0, arguments=consumed
                        )
                    if ended:
                        state = self._TAIL
                    continue
                if isinstance(event, GenerationDone):
                    yield GenerationDone(
                        request_id=rid, finish_reason="tool_calls"
                    )
                    state = self._PASSTHROUGH
                    continue
                yield event
                continue

            if state == self._PARSE_HEADER:
                if isinstance(event, AnswerToken):
                    header_buffer += event.token
                    parsed = self._try_parse_header(header_buffer)
                    if parsed is None:
                        continue
                    tool_name, args_idx = parsed
                    pending_started = None
                    yield ToolCallBegin(
                        request_id=rid,
                        index=0,
                        tool_call_id=f"call_{tool_name}",
                        tool_name=tool_name,
                    )
                    prefix_args = header_buffer[args_idx:]
                    header_buffer = ""
                    consumed, depth, ended = self._scan_args_delta(
                        prefix_args, 0
                    )
                    if consumed:
                        yield ToolCallArgumentDelta(
                            request_id=rid, index=0, arguments=consumed
                        )
                    state = self._TAIL if ended else self._STREAM_ARGS
                    continue
                if isinstance(event, GenerationDone):
                    if pending_started is not None:
                        yield pending_started
                        pending_started = None
                    if header_buffer:
                        yield AnswerToken(request_id=rid, token=header_buffer)
                        header_buffer = ""
                    yield event
                    state = self._PASSTHROUGH
                    continue
                if isinstance(event, ToolCallBegin):
                    if pending_started is not None:
                        yield pending_started
                        pending_started = None
                    if header_buffer:
                        yield AnswerToken(request_id=rid, token=header_buffer)
                        header_buffer = ""
                    state = self._PASSTHROUGH
                    yield event
                    continue
                yield event
                continue

            # UNDECIDED
            if isinstance(event, AnswerStarted):
                pending_started = event
                continue
            if isinstance(event, AnswerToken):
                if event.token.lstrip().startswith("{"):
                    state = self._PARSE_HEADER
                    header_buffer = event.token
                    parsed = self._try_parse_header(header_buffer)
                    if parsed is not None:
                        tool_name, args_idx = parsed
                        pending_started = None
                        yield ToolCallBegin(
                            request_id=rid,
                            index=0,
                            tool_call_id=f"call_{tool_name}",
                            tool_name=tool_name,
                        )
                        prefix_args = header_buffer[args_idx:]
                        header_buffer = ""
                        consumed, depth, ended = self._scan_args_delta(
                            prefix_args, 0
                        )
                        if consumed:
                            yield ToolCallArgumentDelta(
                                request_id=rid, index=0, arguments=consumed
                            )
                        state = self._TAIL if ended else self._STREAM_ARGS
                else:
                    if pending_started is not None:
                        yield pending_started
                        pending_started = None
                    state = self._PASSTHROUGH
                    yield event
                continue
            if isinstance(event, ToolCallBegin):
                pending_started = None
                state = self._PASSTHROUGH
                yield event
                continue
            if isinstance(event, GenerationDone):
                if pending_started is not None:
                    yield pending_started
                    pending_started = None
                yield event
                state = self._PASSTHROUGH
                continue
            yield event

    @classmethod
    def _try_parse_header(cls, text: str) -> tuple[str, int] | None:
        """Ищет ``"name": "..."`` и начало значения ``"arguments":``.

        Возвращает ``(name, args_start_idx)`` — индекс первого символа
        значения arguments (ожидается ``{`` или ``[``). ``None`` — пока
        не хватает текста или структура не подходит.
        """
        stripped = text.lstrip()
        if not stripped.startswith("{"):
            return None
        mname = cls._NAME_RX.search(text)
        if not mname:
            return None
        margs = cls._ARGS_RX.search(text, mname.end())
        if not margs:
            return None
        i = margs.end()
        while i < len(text) and text[i] in " \t\n\r":
            i += 1
        if i >= len(text):
            return None
        if text[i] not in "{[":
            return None
        return (mname.group(1), i)

    @staticmethod
    def _scan_args_delta(
        text: str, start_depth: int
    ) -> tuple[str, int, bool]:
        """Сканирует текст, отслеживая глубину JSON-структуры.

        Учитывает строки и экранирование — ``}`` внутри строки не
        декрементит глубину. Останавливается когда depth достигает 0
        (конец значения ``arguments``). Возвращает
        ``(consumed, new_depth, ended)`` — consumed включает закрывающий
        символ; хвост за ним отбрасывается.
        """
        depth = start_depth
        in_str = False
        escape = False
        for i, ch in enumerate(text):
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
                continue
            if ch in "{[":
                depth += 1
                continue
            if ch in "}]":
                depth -= 1
                if depth <= 0:
                    return text[: i + 1], 0, True
        return text, depth, False


class StopOnFinished(Specification[tuple[AgentContext, AgentEvent]]):
    """Останавливает если генерация завершена и не tool_calls."""

    def check(self, candidate: tuple[AgentContext, AgentEvent]) -> bool:
        _ctx, event = candidate

        if isinstance(event, GenerationDone):
            return event.finish_reason != "tool_calls"

        return False


class StopOnAnyFailure(Specification[tuple[AgentContext, AgentEvent]]):
    """Останавливает цикл при любом терминальном failed-событии.

    Покрывает :class:`GenerationFailed`, :class:`PromptFailed`,
    :class:`PersistenceFailed` — узкие *ToEvent middleware уже сконвертировали
    соответствующие исключения.
    """

    def check(self, candidate: tuple[AgentContext, AgentEvent]) -> bool:
        _ctx, event = candidate
        return isinstance(event, (GenerationFailed, PromptFailed, PersistenceFailed))


class StopOnMaxIterations(Specification[tuple[AgentContext, AgentEvent]]):
    """
    Останавливает если превышен лимит итераций
    """

    def check(self, candidate: tuple[AgentContext, AgentEvent]) -> bool:
        ctx, event = candidate

        if isinstance(event, GenerationDone):
            return ctx.iteration >= ctx.config.max_iterations

        return False
