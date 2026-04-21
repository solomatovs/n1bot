"""Chainlit entrypoint.

Запуск из корня репозитория::

    chainlit run src/chainlit/src/boba/chainlit/app.py

Окружение (BOBA_CONFIG, LITELLM_API_KEY_FILE, WORKSPACE_BASE_DIR и т.п.)
читается :class:`ConfigLoader` — те же переменные, что и у CLI-запуска.
"""

from __future__ import annotations

import asyncio
import logging
from typing import cast

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
    MaxIterationsReached,
    PersistenceFailed,
    PromptFailed,
    RefusalComplete,
    RefusalToken,
    RepeatedFormatFailure,
    ThinkingComplete,
    ThinkingStarted,
    ThinkingToken,
    ToolCallArgumentDelta,
    ToolCallBegin,
    ToolCallComplete,
    ToolCallFormatFailed,
    ToolExecutionFailed,
    ToolResultReady,
    UserNoticeReady,
    UserQueryReceived,
)
from boba.domain.core.workspace import WorkspaceId
from chainlit.input_widget import Select

logger = logging.getLogger(__name__)

# Один контейнер и один AgentHarness-подобный wrapper на процесс: сборка
# DI дорогая, а между сессиями Chainlit общее состояние не нужно — все
# per-session вещи (WorkspaceId) живут в cl.user_session.
_SESSION: ChatSession | None = None


def _get_session() -> ChatSession:
    global _SESSION
    if _SESSION is None:
        _SESSION = ChatSession()
    return _SESSION


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
    agent_task = asyncio.create_task(asyncio.to_thread(_run_agent))
    render_task = asyncio.create_task(_render_events(queue))

    # Агент может упасть (неожиданное исключение — adapter/middleware мог
    # не обернуть его в AgentEvent); поднимаем сразу, не глотая.
    await agent_task
    await render_task


async def _render_events(queue: asyncio.Queue[AgentEvent | None]) -> None:
    """Консюмер очереди событий агента → Chainlit UI.

    Состояние рендеринга (буфер текущего ответа, открытые шаги по
    tool-call'ам и thinking) живёт здесь — в одном async-таске. Это
    избавляет от гонок, которые иначе были бы неизбежны, если держать
    состояние в sink'е на стороне рабочего потока.
    """
    answer_msg: cl.Message | None = None
    thinking_step: cl.Step | None = None
    # index (из ToolCallBegin/Delta) → Step, и tool_call_id → Step — оба
    # нужны, потому что дельты приходят по index, а результат — по id.
    tool_steps_by_index: dict[int, cl.Step] = {}
    tool_steps_by_id: dict[str, cl.Step] = {}
    tool_args_buf: dict[int, list[str]] = {}

    while True:
        event = await queue.get()
        if event is None:
            break

        if isinstance(event, AnswerStarted):
            if answer_msg is None:
                answer_msg = cl.Message(content="")
                await answer_msg.send()

        elif isinstance(event, AnswerToken):
            if answer_msg is None:
                answer_msg = cl.Message(content="")
                await answer_msg.send()
            await answer_msg.stream_token(event.token)

        elif isinstance(event, AnswerDiscarded):
            # Middleware переосмыслил поток как tool call — всё, что мы
            # успели отрисовать как answer, нужно убрать.
            if answer_msg is not None:
                answer_msg.content = ""
                await answer_msg.update()
                answer_msg = None

        elif isinstance(event, AnswerComplete):
            if answer_msg is not None:
                answer_msg.content = event.content
                await answer_msg.update()
                answer_msg = None

        elif isinstance(event, ThinkingStarted):
            thinking_step = cl.Step(name="thinking", type="run")
            thinking_step.input = ""
            await thinking_step.send()

        elif isinstance(event, ThinkingToken):
            if thinking_step is not None:
                await thinking_step.stream_token(event.token)

        elif isinstance(event, ThinkingComplete):
            if thinking_step is not None:
                thinking_step.output = event.content
                await thinking_step.update()
                thinking_step = None

        elif isinstance(event, ToolCallBegin):
            step = cl.Step(name=event.tool_name, type="tool")
            step.input = ""
            await step.send()
            tool_steps_by_index[event.index] = step
            tool_steps_by_id[event.tool_call_id] = step
            tool_args_buf[event.index] = []

        elif isinstance(event, ToolCallArgumentDelta):
            buf = tool_args_buf.get(event.index)
            if buf is not None:
                buf.append(event.arguments)
            step = tool_steps_by_index.get(event.index)
            if step is not None:
                step.input = "".join(tool_args_buf.get(event.index, []))
                await step.update()

        elif isinstance(event, ToolCallComplete):
            step = tool_steps_by_id.get(event.tool_call_id)
            if step is not None:
                step.input = event.arguments
                await step.update()

        elif isinstance(event, ToolResultReady):
            step = tool_steps_by_id.pop(event.tool_call_id, None)
            if step is not None:
                step.output = event.content
                await step.update()

        elif isinstance(event, ToolExecutionFailed):
            step = tool_steps_by_id.pop(event.tool_call_id, None)
            if step is not None:
                step.is_error = True
                step.output = f"[{event.error_kind}] {event.message}"
                await step.update()

        elif isinstance(event, ToolCallFormatFailed):
            # Нет привязки к конкретному step (id/index не определились) —
            # показываем отдельным сообщением об ошибке.
            await cl.Message(
                content=f"**Tool call format error** `{event.error_kind}`: {event.message}",
                author="system",
            ).send()

        elif isinstance(event, UserNoticeReady):
            await cl.Message(
                content=f"**{event.severity}**: {event.message}",
                author="system",
            ).send()

        elif isinstance(event, RefusalToken):
            if answer_msg is None:
                answer_msg = cl.Message(content="")
                await answer_msg.send()
            await answer_msg.stream_token(event.token)

        elif isinstance(
            event,
            (
                GenerationFailed,
                PromptFailed,
                PersistenceFailed,
                MaxIterationsReached,
                RepeatedFormatFailure,
            ),
        ):
            kind = type(event).__name__
            msg = getattr(event, "message", "")
            error_kind = getattr(event, "error_kind", "")
            await cl.Message(
                content=f"**{kind}** `{error_kind}`: {msg}",
                author="system",
            ).send()

        elif isinstance(
            event,
            (
                UserQueryReceived,
                GenerationDone,
                RefusalComplete,
            ),
        ):
            # Служебные события — UI не нужен (stop-сигналы, already-emitted).
            pass

        else:
            # Неизвестный тип — в лог, но не роняем UI.
            logger.warning("unhandled agent event: %r", event)

    # Графия могла остаться открытой, если поток завершился посреди
    # генерации (обычно уже закрылось AnswerComplete/ToolResult*).
    if answer_msg is not None:
        await answer_msg.update()
    if thinking_step is not None:
        await thinking_step.update()
    for step in tool_steps_by_id.values():
        await step.update()
