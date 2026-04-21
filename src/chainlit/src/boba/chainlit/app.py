"""Chainlit entrypoint.

Запуск из корня репозитория::

    chainlit run src/chainlit/src/boba/chainlit/app.py

Окружение (BOBA_CONFIG, LITELLM_API_KEY_FILE, WORKSPACE_BASE_DIR и т.п.)
читается :class:`ConfigLoader` — те же переменные, что и у CLI-запуска.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any, cast

import chainlit as cl
from boba.chainlit.bridge import ChainlitBridgeSink
from boba.chainlit.config import load_models
from boba.chainlit.files import save_upload
from boba.chainlit.session import ChatSession
from boba.domain.agent.events import (
    AgentEvent,
    AnswerComplete,
    AnswerDiscarded,
    AnswerStarted,
    AnswerToken,
    GenerationDone,
    GenerationFailed,
    GenerationStarted,
    IterationStarted,
    LLMRequestSent,
    MaxIterationsReached,
    PersistenceFailed,
    PromptFailed,
    RefusalComplete,
    RefusalToken,
    RepeatedFormatFailure,
    StageCompleted,
    StageStarted,
    ThinkingComplete,
    ThinkingStarted,
    ThinkingToken,
    ToolCallArgumentDelta,
    ToolCallBegin,
    ToolCallComplete,
    ToolCallFormatFailed,
    ToolExecutionFailed,
    ToolExecutionStarted,
    ToolResultReady,
    UserNoticeReady,
    UserQueryReceived,
)
from boba.domain.core.patterns import FirstMatchDispatcher, Specification
from boba.domain.core.workspace import WorkspaceId
from chainlit.input_widget import Select

logger = logging.getLogger(__name__)

# Один контейнер и один AgentHarness-подобный wrapper на процесс: сборка
# DI дорогая, а между сессиями Chainlit общее состояние не нужно — все
# per-session вещи (WorkspaceId) живут в cl.user_session. functools.cache
# даёт ленивую инициализацию без модульного global.
@functools.cache
def _get_session() -> ChatSession:
    return ChatSession()


@cl.on_chat_start
async def on_chat_start() -> None:
    """Новый WorkspaceId на каждую сессию чата.

    History-workspace внутри WorkspaceId хранит ``messages.jsonl`` — это и
    есть память диалога: последующие сообщения в той же сессии видят
    контекст предыдущих.
    """
    workspace_id = WorkspaceId.new()
    cl.user_session.set("workspace_id", workspace_id)
    # Прогрев контейнера в фоне — первая отправка сообщения не будет
    # висеть несколько секунд на загрузке конфига.
    await asyncio.to_thread(_get_session)

    # ChatSettings — единственный источник модели для агентского лупа.
    # Список берём из [chainlit] models; пустой список — misconfiguration,
    # падаем громко, чтобы не уйти в неявный fallback на конфиг.
    # Порядок отображения == порядок в TOML; «дефолта» нет — в виджете
    # подсвечен первый элемент, пользователь при необходимости меняет.
    models = load_models()
    if not models:
        msg = "[chainlit] models пуст или не задан — UI не может выбрать модель"
        raise RuntimeError(msg)
    await cl.ChatSettings(
        [
            Select(
                id="model",
                label="LLM модель",
                values=models,
                initial_index=0,
            ),
        ],
    ).send()
    # ChatSettings.send() не триггерит on_settings_update, поэтому
    # явно синхронизируем выбор с тем, что показано в UI.
    cl.user_session.set("model", models[0])

    await cl.Message(
        content=f"Сессия готова. workspace_id = `{workspace_id.to_wire()}`",
        author="system",
    ).send()


@cl.on_settings_update
async def on_settings_update(settings: dict[str, object]) -> None:
    """Сохранить выбранную в шестерёнке модель для последующих запросов."""
    model = settings.get("model")
    if isinstance(model, str) and model:
        cl.user_session.set("model", model)


@cl.on_message
async def on_message(message: cl.Message) -> None:
    workspace_id = cast(WorkspaceId, cl.user_session.get("workspace_id"))
    model = cast(str, cl.user_session.get("model"))
    session = _get_session()

    # Аттачменты из композера — сохраняем в тот же project-workspace, из
    # которого читают file-tools агента. Shell берём из того же реестра,
    # что и session.run → get-or-create по workspace_id возвращает один
    # и тот же инстанс, так что agent увидит файл в корне workspace.
    saved: list[str] = []
    if message.elements:
        shell = session.project_workspace(workspace_id)
        for el in message.elements:
            src_path = getattr(el, "path", None)
            name = getattr(el, "name", None)
            if not src_path or not name:
                continue
            rel = await asyncio.to_thread(save_upload, shell, src_path, name)
            saved.append(rel)

    # Явно сообщаем агенту о прикреплённых файлах — иначе он видит только
    # текст и не знает, что смотреть (UI-bubble аттачмента в query не
    # попадает). Имена уже в workspace root, так что `cat <name>` работает.
    query = message.content
    if saved:
        listing = ", ".join(saved)
        query = f"{query}\n\n[attached files in workspace root: {listing}]"

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[AgentEvent | None] = asyncio.Queue()
    bridge = ChainlitBridgeSink(loop, queue)

    def _run_agent() -> None:
        try:
            session.run(workspace_id, query, bridge, model=model)
        finally:
            bridge.close()

    # Агент работает в worker-потоке; async-таск рисует UI по событиям.
    # gather, а не последовательные await: если агент бросит, render_task
    # иначе остаётся висеть (bridge.close() из finally пошлёт None, но мы
    # уже перестали слушать). gather'им обе и пробрасываем первое
    # исключение, не глотая.
    await asyncio.gather(
        asyncio.to_thread(_run_agent),
        _render_events(queue),
    )


class _IsType(Specification[AgentEvent]):
    """isinstance-предикат для маршрутизации AgentEvent по типу.

    Локальный вариант — общего ``IsType`` в patterns.py нет,
    :class:`IsInstance` там специфичен для исключений.
    """

    def __init__(self, *types: type[AgentEvent]) -> None:
        self._types = types

    def check(self, candidate: AgentEvent) -> bool:
        return isinstance(candidate, self._types)


_TERMINAL_ERRORS = (
    GenerationFailed,
    PromptFailed,
    PersistenceFailed,
    MaxIterationsReached,
    RepeatedFormatFailure,
)

# Техимена стадий (из domain/agent/meat/*.name()) в человеческий текст.
# Неизвестные стадии показываются как есть — чтобы при появлении новой
# было видно, что она есть, но не сломало UI.
_STAGE_LABELS: dict[str, str] = {
    "UserPrompt": "Готовлю запрос…",
}

_GENERATION_STATUS = "Модель обрабатывает запрос…"


def _stage_label(stage: str) -> str:
    return _STAGE_LABELS.get(stage, stage)


def _llm_request_status(event: LLMRequestSent) -> str:
    tools_hint = " с tools" if event.has_tools else ""
    return (
        f"Жду ответ от модели `{event.model}`{tools_hint}… "
        f"(сообщений в контексте: {event.messages_count})"
    )


class _EventRenderer:
    """Состояние рендеринга + обработчики событий агента.

    Per-request инстанс: состояние (буфер ответа, открытые step'ы) живёт
    здесь. Один async-таск ведёт эту машину — гонок между обработчиками
    нет по построению. Маршрутизация вынесена в :class:`FirstMatchDispatcher`
    из ``domain/core/patterns``, см. :func:`_build_dispatcher`.
    """

    def __init__(self) -> None:
        self.answer_msg: cl.Message | None = None
        self.thinking_step: cl.Step | None = None
        # Один transient-индикатор прогресса. Stage/Generation события
        # его обновляют на месте, «настоящий» контент (токены ответа,
        # tool call, thinking, ошибки) — удаляет, чтобы в истории чата
        # не копились технические «Готовлю запрос / Модель обрабатывает».
        self.status_msg: cl.Message | None = None
        # index (из ToolCallBegin/Delta) → Step, и tool_call_id → Step.
        # Оба нужны: дельты приходят по index, результат — по id.
        self.tool_steps_by_index: dict[int, cl.Step] = {}
        self.tool_steps_by_id: dict[str, cl.Step] = {}
        # Накопленный JSON tool-аргументов по index. Строка, а не list —
        # join на каждой дельте был бы O(N²) по длине аргумента.
        self.tool_args_buf: dict[int, str] = {}

    async def _set_status(self, text: str) -> None:
        """Показать/обновить transient-статус. Одна реплика на всю
        сессию ожидания — replace-in-place вместо нового пузыря."""
        if self.status_msg is None:
            self.status_msg = cl.Message(content=text, author="system")
            await self.status_msg.send()
        else:
            self.status_msg.content = text
            await self.status_msg.update()

    async def _clear_status(self) -> None:
        """Убрать transient-статус перед тем, как показать реальный
        контент. Вызывается из всех «значимых» handler'ов."""
        if self.status_msg is not None:
            await self.status_msg.remove()
            self.status_msg = None

    async def _open_answer(self) -> cl.Message:
        # Токены ответа — реальный контент, статус-плашка ему больше
        # не нужна.
        await self._clear_status()
        if self.answer_msg is None:
            self.answer_msg = cl.Message(content="")
            await self.answer_msg.send()
        return self.answer_msg

    async def on_answer_started(self, event: AnswerStarted) -> None:
        del event
        await self._open_answer()

    async def on_answer_token(self, event: AnswerToken) -> None:
        msg = await self._open_answer()
        await msg.stream_token(event.token)

    async def on_answer_discarded(self, event: AnswerDiscarded) -> None:
        # Middleware переосмыслил поток как tool call — всё, что мы
        # успели отрисовать как answer, нужно убрать.
        del event
        if self.answer_msg is not None:
            self.answer_msg.content = ""
            await self.answer_msg.update()
            self.answer_msg = None

    async def on_answer_complete(self, event: AnswerComplete) -> None:
        # Токены уже в UI через stream_token; content переписывать
        # значит дёргать полную перерисовку (мигание). Отпускаем
        # ссылку, чтобы следующий AnswerStarted/Token создал новое
        # сообщение.
        del event
        self.answer_msg = None

    async def on_thinking_started(self, event: ThinkingStarted) -> None:
        # Thinking — реальный контент, statusbar больше не нужен.
        del event
        await self._clear_status()
        self.thinking_step = cl.Step(name="thinking", type="run")
        self.thinking_step.input = ""
        await self.thinking_step.send()

    async def on_thinking_token(self, event: ThinkingToken) -> None:
        if self.thinking_step is not None:
            await self.thinking_step.stream_token(event.token)

    async def on_thinking_complete(self, event: ThinkingComplete) -> None:
        del event
        self.thinking_step = None

    async def on_tool_begin(self, event: ToolCallBegin) -> None:
        # Tool call — реальный контент, убираем transient-статус.
        await self._clear_status()
        step = cl.Step(name=event.tool_name, type="tool")
        step.input = ""
        await step.send()
        self.tool_steps_by_index[event.index] = step
        self.tool_steps_by_id[event.tool_call_id] = step
        self.tool_args_buf[event.index] = ""

    async def on_tool_arg_delta(self, event: ToolCallArgumentDelta) -> None:
        # Конкатенируем и отдаём инкрементальный snapshot в UI.
        # cl.Step не умеет stream_token в .input, так что приходится
        # переустанавливать поле целиком; строковый += — O(N) на дельту,
        # list+join давал O(N²).
        self.tool_args_buf[event.index] = (
            self.tool_args_buf.get(event.index, "") + event.arguments
        )
        step = self.tool_steps_by_index.get(event.index)
        if step is not None:
            step.input = self.tool_args_buf[event.index]
            await step.update()

    async def on_tool_complete(self, event: ToolCallComplete) -> None:
        # Дельты уже собрали полный arguments; Complete — только
        # стоп-сигнал, дополнительный update() — лишний сетевой тик.
        del event

    async def on_tool_exec_started(self, event: ToolExecutionStarted) -> None:
        # Tool начал исполняться — меняем индикацию step'а с «args
        # дописываются» на «running». Без этого медленные tools (fs,
        # http) выглядят как зависший шаг с аргументами.
        step = self.tool_steps_by_id.get(event.tool_call_id)
        if step is not None:
            step.output = "⏳ выполняется…"
            await step.update()

    async def on_tool_result(self, event: ToolResultReady) -> None:
        step = self.tool_steps_by_id.pop(event.tool_call_id, None)
        if step is not None:
            step.output = event.content
            await step.update()

    async def on_tool_exec_failed(self, event: ToolExecutionFailed) -> None:
        step = self.tool_steps_by_id.pop(event.tool_call_id, None)
        if step is not None:
            step.is_error = True
            step.output = f"[{event.error_kind}] {event.message}"
            await step.update()

    async def on_tool_format_failed(self, event: ToolCallFormatFailed) -> None:
        # Нет привязки к конкретному step (id/index не определились) —
        # показываем отдельным сообщением об ошибке.
        await self._clear_status()
        await cl.Message(
            content=(
                f"**Tool call format error** "
                f"`{event.error_kind}`: {event.message}"
            ),
            author="system",
        ).send()

    async def on_notice(self, event: UserNoticeReady) -> None:
        await self._clear_status()
        await cl.Message(
            content=f"**{event.severity}**: {event.message}",
            author="system",
        ).send()

    async def on_refusal_token(self, event: RefusalToken) -> None:
        msg = await self._open_answer()
        await msg.stream_token(event.token)

    async def on_refusal_complete(self, event: RefusalComplete) -> None:
        # Параллельно AnswerComplete: токены уже застримлены в UI,
        # только отпускаем ссылку.
        del event
        self.answer_msg = None

    async def on_stage_started(self, event: StageStarted) -> None:
        # Верхнеуровневая фаза цикла. Обновляем transient-индикатор,
        # не создавая отдельного Step'а — иначе в истории чата копятся
        # технические «UserPrompt», которые пользователю ничего не
        # говорят. Неизвестные стадии показываются как есть.
        await self._set_status(_stage_label(event.stage))

    async def on_stage_completed(self, event: StageCompleted) -> None:
        # Фаза закрыта — текущий статус («Готовлю запрос…») больше не
        # соответствует реальности. Убираем плашку; следующий значимый
        # event (LLMRequestSent / GenerationStarted / ...) сам поставит
        # актуальный текст. detail из домена ("user prompt added") —
        # техническая служебка, пользователю её не показываем.
        del event
        await self._clear_status()

    async def on_llm_request_sent(self, event: LLMRequestSent) -> None:
        # HTTP-запрос ушёл, ждём TTFT. Заменяет generic-статус
        # подробным (модель, наличие tools, размер контекста).
        await self._set_status(_llm_request_status(event))

    async def on_generation_started(self, event: GenerationStarted) -> None:
        # Первый чанк пошёл — статус держим, токены ответа/thinking
        # всё равно _clear_status() при первом токене.
        del event
        await self._set_status(_GENERATION_STATUS)

    async def on_iteration_started(self, event: IterationStarted) -> None:
        # Первую итерацию не маркируем — пользователь и так знает,
        # что только что отправил запрос. Со второй уже полезно: в
        # длинных tool-цепочках видно, сколько прогресса осталось.
        if event.iteration <= 1:
            return
        await self._set_status(
            f"Итерация {event.iteration}/{event.max_iterations}…"
        )

    async def on_generation_done(self, event: GenerationDone) -> None:
        # Если после этого пойдут AnswerToken/ToolCallBegin — они сами
        # удалят статус. Если генерация закончилась без контента
        # (terminal error / пустой ответ) — плашку тоже убираем, чтобы
        # не висела на экране.
        del event
        await self._clear_status()

    async def on_user_query(self, event: UserQueryReceived) -> None:
        # Пользователь уже видит свою реплику в UI — рендерить ещё раз
        # значит дублировать. Явно no-op.
        del event

    async def on_terminal_error(self, event: AgentEvent) -> None:
        await self._clear_status()
        kind = type(event).__name__
        msg_text = cast(str, getattr(event, "message", ""))
        error_kind = cast(str, getattr(event, "error_kind", ""))
        await cl.Message(
            content=f"**{kind}** `{error_kind}`: {msg_text}",
            author="system",
        ).send()

    async def on_unknown(self, event: AgentEvent) -> None:
        logger.warning("unhandled agent event: %r", event)


_RenderRoute = Callable[[AgentEvent], Awaitable[None]]


def _build_dispatcher(r: _EventRenderer) -> FirstMatchDispatcher[AgentEvent, Any]:
    """Собрать маршруты event-type → handler.

    Возвращаемый диспетчер — callable ``AgentEvent → Awaitable[None]``;
    ``FirstMatchDispatcher`` параметризуется вторым типом как ``Any``,
    потому что его сигнатура скрывает asynchrony за generic TOut.

    Каждый handler типизирован узким подклассом ``AgentEvent`` — это
    удобно для тела, но ``Callable`` контравариантен по аргументу, так
    что присвоение в ``_RenderRoute`` требует ``cast``. Безопасность
    типов держится на том, что ``_IsType(T)`` пропускает в маршрут
    только ``T``, так что узкий параметр handler'а гарантированно
    совпадает с фактическим.
    """
    def route(
        types: type[AgentEvent] | tuple[type[AgentEvent], ...],
        handler: Callable[..., Awaitable[None]],
    ) -> tuple[Specification[AgentEvent], _RenderRoute]:
        specs = types if isinstance(types, tuple) else (types,)
        return (_IsType(*specs), cast(_RenderRoute, handler))

    routes: list[tuple[Specification[AgentEvent], _RenderRoute]] = [
        route(AnswerStarted, r.on_answer_started),
        route(AnswerToken, r.on_answer_token),
        route(AnswerDiscarded, r.on_answer_discarded),
        route(AnswerComplete, r.on_answer_complete),
        route(ThinkingStarted, r.on_thinking_started),
        route(ThinkingToken, r.on_thinking_token),
        route(ThinkingComplete, r.on_thinking_complete),
        route(ToolCallBegin, r.on_tool_begin),
        route(ToolCallArgumentDelta, r.on_tool_arg_delta),
        route(ToolCallComplete, r.on_tool_complete),
        route(ToolExecutionStarted, r.on_tool_exec_started),
        route(ToolResultReady, r.on_tool_result),
        route(ToolExecutionFailed, r.on_tool_exec_failed),
        route(ToolCallFormatFailed, r.on_tool_format_failed),
        route(UserNoticeReady, r.on_notice),
        route(RefusalToken, r.on_refusal_token),
        route(RefusalComplete, r.on_refusal_complete),
        route(StageStarted, r.on_stage_started),
        route(StageCompleted, r.on_stage_completed),
        route(IterationStarted, r.on_iteration_started),
        route(LLMRequestSent, r.on_llm_request_sent),
        route(GenerationStarted, r.on_generation_started),
        route(GenerationDone, r.on_generation_done),
        route(UserQueryReceived, r.on_user_query),
        route(_TERMINAL_ERRORS, r.on_terminal_error),
    ]
    return FirstMatchDispatcher[AgentEvent, Any](routes, r.on_unknown)


async def _render_events(queue: asyncio.Queue[AgentEvent | None]) -> None:
    """Консюмер очереди событий агента → Chainlit UI.

    Вся диспетчеризация — в :func:`_build_dispatcher` через
    :class:`FirstMatchDispatcher` из ``domain/core/patterns``. Здесь
    остаётся только цикл по очереди и выход по sentinel.
    """
    renderer = _EventRenderer()
    dispatch = _build_dispatcher(renderer)

    while True:
        event = await queue.get()
        if event is None:
            break
        await dispatch(event)

    # Cleanup'а не требуется: все токены уже отрисованы через
    # stream_token. Если *Complete не пришёл (оборвался поток) —
    # оставляем UI как есть; повторный update() без изменений полей
    # всё равно ничего не добавит и только сгенерирует сетевой ping.
