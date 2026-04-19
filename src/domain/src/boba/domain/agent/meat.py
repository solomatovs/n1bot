"""Middleware-слои AgentLoop."""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import Enum, auto
from typing import ClassVar

from boba.domain.agent.errors import (
    AgentError,
    LLMError,
    LLMFeedbackError,
    LLMResponseFormatError,
    Retryable,
    ToolFeedbackError,
    UserNoticeError,
)
from boba.domain.agent.events import (
    AgentEvent,
    AnswerComplete,
    AnswerDiscarded,
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
    ToolExecutionFailed,
    ToolResultReady,
    UserNoticeReady,
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
    Converter,
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
)
from boba.domain.core.tools import (
    ToolCall,
    ToolExecutionError,
    ToolId,
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

    def stream(self, ctx: AgentContext) -> Iterator[AgentEvent]:  # noqa: C901
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
                case AnswerDiscarded(request_id=rid):
                    self._answer[rid].clear()
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
    - ``ToolResultReady`` → ``LLMMessage(role="tool", content, tool_call_id)``;
    - ``ToolExecutionFailed`` → ``LLMMessage(role="tool", message, tool_call_id)``
      (LLM должна увидеть ошибку tool'а при реплее так же, как увидела
      бы её при live-исполнении).

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
                case ToolExecutionFailed(tool_call_id=tid, message=m):
                    self._message_service.add(
                        LLMMessage(role="tool", content=m, tool_call_id=tid)
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

    1. Парсит JSON ``arguments``. Битый JSON → :class:`ToolFeedbackError`
       (LLM увидит ошибку и сможет починить).
    2. Вызывает :meth:`ToolsService.execute`. Сервис бросает «сырую»
       :class:`ToolExecutionError` (без знания про tool_call_id) — тут
       обогащается идентификатором вызова и пробрасывается как
       :class:`ToolFeedbackError`.
    3. Успех → ``LLMMessage(role="tool", tool_call_id=..., content=...)`` +
       :class:`ToolResultReady`.

    Batch-семантика делегирована :meth:`AgentErrorRouter.run_batch`:
    успешные события стримятся сразу, :class:`LLMFeedbackError` из
    любой подзадачи копится и маршрутизируется в конце — так одна
    упавшая подзадача не обрывает остальные. Терминальные ошибки
    пропускаются наверх во внешний :class:`AgentErrorRouterMiddleware`.

    Если LLM не запросила тулов — middleware просто проксирует события
    inner без побочных эффектов.
    """

    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
        tools_service: ToolsService,
        message_service: MessageService,
        error_router: AgentErrorRouter,
    ) -> None:
        self._inner = inner
        self._tools_service = tools_service
        self._message_service = message_service
        self._error_router = error_router

    def name(self) -> str:
        return "ToolExecution"

    def stream(self, ctx: AgentContext) -> Iterator[AgentEvent]:
        pending: list[ToolCallComplete] = []

        for event in self._inner.stream(ctx):
            yield event
            if isinstance(event, ToolCallComplete):
                pending.append(event)

        yield from self._error_router.run_batch(
            ctx, (self._run_tool(tc) for tc in pending)
        )

    def _run_tool(self, tc: ToolCallComplete) -> Iterator[AgentEvent]:
        try:
            arguments = json.loads(tc.arguments)
            call = ToolCall(
                tool_id=ToolId(tc.tool_name),
                arguments=arguments,
            )
            result = self._tools_service.execute(None, call)
        except json.JSONDecodeError as e:
            raise ToolFeedbackError(
                tool_call_id=tc.tool_call_id,
                tool_name=tc.tool_name,
                error_kind=type(e).__name__,
                message=f"invalid JSON arguments: {e}",
            ) from e
        except ToolExecutionError as e:
            raise ToolFeedbackError(
                tool_call_id=tc.tool_call_id,
                tool_name=tc.tool_name,
                error_kind=type(e).__name__,
                message=e.message,
            ) from e

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


class AgentErrorRouter:
    """Централизованная маршрутизация :class:`AgentError` в события и
    побочные эффекты.

    Единая точка знания «какой тип ошибки → какое событие + какой сайд-
    эффект». Middleware, поймавший :class:`AgentError`, делегирует сюда.

    Две API-точки для клиентов:

    - :meth:`route` — разобрать одну ошибку (вызывается верхнеуровневым
      :class:`AgentErrorRouterMiddleware` из top-level try/except).
    - :meth:`run_batch` — прогнать серию подзадач-генераторов с
      автоматическим сбором :class:`LLMFeedbackError` и маршрутизацией
      в конце. Batch-middleware (напр. :class:`ToolExecutionMiddleware`)
      **не пишет** свой try/except — просто передают генераторы сюда.

    Маршруты (match-case):

    - :class:`LLMError` → :class:`GenerationFailed` (``status_code``,
      ``retryable`` по маркеру :class:`Retryable`). Терминально.
    - :class:`PromptError` → :class:`PromptFailed` (``provider``).
      Терминально.
    - :class:`HistoryError` → :class:`PersistenceFailed`. Терминально.
    - :class:`ToolFeedbackError` → запись ``LLMMessage(role="tool",
      tool_call_id=..., content=...)`` в :class:`MessageService` +
      :class:`ToolExecutionFailed`. Не терминально.
    - :class:`UserNoticeError` → :class:`UserNoticeReady`. Не терминально.
    - Непокрытый подкласс → :class:`TypeError` (баг: забыли добавить
      ветку).
    """

    def __init__(self, message_service: MessageService) -> None:
        self._message_service = message_service

    def route(self, ctx: AgentContext, err: AgentError) -> Iterator[AgentEvent]:
        rid = ctx.request.request_id
        retryable = isinstance(err, Retryable)
        kind = type(err).__name__
        msg = str(err)
        match err:
            case LLMError():
                yield GenerationFailed(
                    request_id=rid,
                    error_kind=kind,
                    message=msg,
                    retryable=retryable,
                    status_code=err.status_code,
                )
            case PromptError():
                yield PromptFailed(
                    request_id=rid,
                    error_kind=kind,
                    message=msg,
                    retryable=retryable,
                    provider=err.provider,
                )
            case HistoryError():
                yield PersistenceFailed(
                    request_id=rid,
                    error_kind=kind,
                    message=msg,
                    retryable=retryable,
                )
            case ToolFeedbackError():
                self._message_service.add(
                    LLMMessage(
                        role="tool",
                        content=err.message,
                        tool_call_id=err.tool_call_id,
                    ),
                )
                yield ToolExecutionFailed(
                    request_id=rid,
                    tool_call_id=err.tool_call_id,
                    tool_name=err.tool_name,
                    error_kind=err.error_kind,
                    message=err.message,
                )
            case UserNoticeError():
                yield UserNoticeReady(
                    request_id=rid,
                    message=err.message,
                    severity=err.severity,
                )
            case _:
                raise TypeError(
                    f"AgentErrorRouter: unmapped AgentError subclass "
                    f"{kind!r}. Добавь ветку в route или унаследуй новый тип "
                    f"от одного из известных семейств."
                ) from err

    def run_batch(
        self,
        ctx: AgentContext,
        tasks: Iterable[Iterator[AgentEvent]],
    ) -> Iterator[AgentEvent]:
        """Прогоняет серию подзадач-генераторов с batch-семантикой для
        :class:`LLMFeedbackError`.

        События успешных подзадач стримятся сразу (``yield from``).
        Ошибка уровня :class:`LLMFeedbackError` из любой подзадачи
        **не обрывает** остальные: ловится, копится, маршрутизируется
        после завершения всей серии (в порядке возникновения).

        :class:`TerminalError` (и любое не-``LLMFeedbackError``
        ``AgentError``) — пропускаем наверх немедленно: при сбое LLM /
        persistence дальше работать смысла нет, batch прерывается.

        API задуман так, чтобы middleware-автор не писал свой
        ``try/except`` и не думал про batch — просто передаёт
        генераторы подзадач::

            yield from self._error_router.run_batch(
                ctx, (self._run_task(item) for item in items)
            )
        """
        deferred: list[LLMFeedbackError] = []
        for task in tasks:
            try:
                yield from task
            except LLMFeedbackError as e:
                deferred.append(e)
        for err in deferred:
            yield from self.route(ctx, err)


class AgentErrorRouterMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Верхнеуровневый try/except над всем source-chain'ом.

    Ставится **самым внешним** слоем (поверх retry, промптов, history,
    tool-execution). Любой middleware глубже может ``raise`` подкласс
    :class:`AgentError`, не зная, что будет дальше — средний слой
    прокидывает, этот ловит и делегирует :class:`AgentErrorRouter`.

    Замечание про batch-middleware: они ловят :class:`LLMFeedbackError`
    **сами** (per-iteration) и вызывают :meth:`AgentErrorRouter.route`
    напрямую — чтобы одна упавшая подзадача не оборвала остальные.
    Досюда из батч-middleware долетают только терминальные ошибки.

    Всё, что не наследует :class:`AgentError` (``KeyError``, ``TypeError``
    и т.п.), проходит насквозь и крашит процесс — баги не маскируем.
    :class:`Retryable`-подклассы сюда доезжают только после исчерпания
    попыток во внутреннем retry-слое.
    """

    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
        router: AgentErrorRouter,
    ) -> None:
        self._inner = inner
        self._router = router

    def name(self) -> str:
        return "AgentErrorRouter"

    def stream(self, ctx: AgentContext) -> Iterator[AgentEvent]:
        try:
            yield from self._inner.stream(ctx)
        except AgentError as e:
            yield from self._router.route(ctx, e)


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
    - Любое отклонение → :class:`LLMResponseFormatError` с описанием,
      что именно не так (невалидный JSON, не-объект на корне,
      отсутствующие/посторонние поля, неверные типы).

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
            raise LLMResponseFormatError(
                f"Ответ модели начался с '{{', значит обязан быть "
                f"корректным JSON tool call, но JSON невалиден: "
                f"{e.msg} (позиция {e.pos}). Полученный content: {value!r}"
            ) from e

        if not isinstance(data, dict):
            raise LLMResponseFormatError(
                f"Ожидался JSON-объект tool call вида "
                f"{{'name': str, 'arguments': object}}, получено значение "
                f"типа {type(data).__name__}: {value!r}"
            )

        missing = sorted(self._ALLOWED - data.keys())
        if missing:
            raise LLMResponseFormatError(
                f"В JSON tool call отсутствуют обязательные поля: "
                f"{missing}. Ожидался формат "
                f"{{'name': str, 'arguments': object}}. "
                f"Полученный content: {value!r}"
            )

        extra = sorted(data.keys() - self._ALLOWED)
        if extra:
            raise LLMResponseFormatError(
                f"В JSON tool call присутствуют посторонние поля: "
                f"{extra}. Допустимы только {sorted(self._ALLOWED)}. "
                f"Любые дополнительные поля (например 'content', 'response', "
                f"'type', 'id', 'thought') запрещены — текст для пользователя "
                f"нельзя передавать внутри tool call. "
                f"Полученный content: {value!r}"
            )

        name = data["name"]
        if not isinstance(name, str) or not name.strip():
            raise LLMResponseFormatError(
                f"Поле 'name' должно быть непустой строкой, получено "
                f"{type(name).__name__}={name!r}. Полученный content: {value!r}"
            )

        arguments = data["arguments"]
        if not isinstance(arguments, (dict, list)):
            raise LLMResponseFormatError(
                f"Поле 'arguments' (tool '{name}') должно быть объектом или "
                f"массивом, получено {type(arguments).__name__}={arguments!r}. "
                f"Полученный content: {value!r}"
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

      * Успех → :class:`ToolCallBegin` + :class:`ToolCallArgumentDelta`
        + :class:`GenerationDone` с ``finish_reason="tool_calls"``.
      * Парсер бросил :class:`LLMResponseFormatError` — исключение
        прокидывается наружу (через retry-слой до
        :class:`ErrorToEventMiddleware`, который конвертирует его в
        терминальное :class:`GenerationFailed`).

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
                        yield GenerationDone(
                            request_id=rid, finish_reason="tool_calls"
                        )
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
        Сигналим downstream-ам отбросить накопленный content и передаём
        буфер парсеру; его исключение летит наружу через ``yield from``.
        """
        yield AnswerDiscarded(request_id=rid)
        parsed = self._parser.convert(raw)
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
