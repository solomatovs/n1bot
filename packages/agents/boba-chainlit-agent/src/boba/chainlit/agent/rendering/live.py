""" """

from __future__ import annotations

import chainlit as cl
from chainlit.utils import utc_now

from boba.agent.events import DiagnosticEvent
from boba.chainlit.agent.rendering.dispatcher import EventRenderTarget

__all__ = ["ChainlitLiveTarget"]


class ChainlitLiveTarget(EventRenderTarget):
    """Реализует `EventRenderTarget` для текущей chainlit-сессии.

    Параметр `diagnostic` управляет показом `DiagnosticEvent`-ов:
    False - тихо игнорируем (классический чистый чат);
    True  - рендерим как сворачиваемый cl.Step `diag: {topic}`.
    Toggle прокидывается из callbacks.py (берётся из user_session).
    """

    def __init__(
        self,
        parent_id: str | None,
        *,
        diagnostic: bool = False,
    ) -> None:
        self._parent_id = parent_id
        self._diagnostic = diagnostic
        self._answer_msg: cl.Message | None = None
        self._thinking_step: cl.Step | None = None
        # Status — cl.Step(type="run"), не cl.Message: статус не должен
        # занимать слот в основной chat-ленте и влиять на ordering.
        self._status_step: cl.Step | None = None
        # tool_call_id → Step.
        self._tool_steps_by_id: dict[str, cl.Step] = {}

    # --- streaming ---------------------------------------------------

    async def answer_chunk(self, text: str) -> None:
        msg = await self._open_answer()
        await msg.stream_token(text)

    async def thinking_chunk(self, text: str) -> None:
        step = await self._open_thinking()
        await step.stream_token(text)

    async def refusal_chunk(self, text: str) -> None:
        msg = await self._open_answer()
        await msg.stream_token(text)

    # --- snapshot завершения ----------------------------------------

    async def user_query(self, text: str) -> None:
        # Chainlit уже отрисовал ввод из cl.Message пользователя.
        del text

    async def answer_complete(self, text: str) -> None:
        if self._answer_msg is None and text:
            # Snapshot пришёл без предшествующих delta'ов (теоретически
            # возможно, если адаптер LLM эмитит только финальный текст).
            msg = cl.Message(content=text, created_at=utc_now())
            await msg.send()
            return
        # Токены уже отрисованы потоково — просто закрываем сообщение.
        self._answer_msg = None

    async def thinking_complete(self, text: str) -> None:
        if self._thinking_step is None and text:
            step = cl.Step(name="thinking", type="run", parent_id=self._parent_id)
            await step.send()
            await _finalize_step(step, text)
            return
        self._thinking_step = None

    async def refusal_complete(self, text: str) -> None:
        if self._answer_msg is None and text:
            msg = cl.Message(content=text, created_at=utc_now())
            await msg.send()
            return
        self._answer_msg = None

    async def tool_result(
        self,
        call_id: str,
        text: str,
        *,
        is_error: bool,
    ) -> None:
        step = self._tool_steps_by_id.pop(call_id, None)
        if step is None:
            # Orphan: result без открытого tool_step (ToolExecutionStarted
            # не пришёл) — рисуем отдельным шагом, чтобы не потерять текст.
            step = cl.Step(name="tool_result", type="tool", parent_id=self._parent_id)
            await step.send()
        await _finalize_step(step, text, is_error=is_error)

    async def feedback(self, text: str) -> None:
        await cl.Message(
            content=f"**Feedback to LLM**:\n\n{text}",
            author="system",
            created_at=utc_now(),
        ).send()

    # --- phase-маркеры ------------------------------------------------

    async def iteration_started(self) -> None:
        # Между итерациями обнуляем ссылки на answer/thinking, иначе
        # stream_token из новой итерации пишет в UI из старой.
        await self._drop_pending_answer()
        self._thinking_step = None

    async def tool_started(
        self,
        call_id: str,
        name: str,
        args_json: str,
    ) -> None:
        # Жизненный цикл tool-step'а в UI начинается ЗДЕСЬ
        # (не на ToolCallMessage). Финализируется по tool_result /
        # tool_execution_failed по тому же call_id.
        await self._clear_status()
        step = cl.Step(name=name, type="tool", parent_id=self._parent_id)
        await step.send()
        await step.stream_token(args_json, is_input=True)
        await _finalize_step(step, "⏳ выполняется…")
        self._tool_steps_by_id[call_id] = step

    async def generation_milestone(self) -> None:
        # UI-сущности (answer cl.Message, thinking cl.Step) создаём ЛЕНИВО
        # в *_chunk при первом реальном токене, а не на терминаторе генерации:
        # pre-emptive send() ломает порядок в timeline.
        await self._clear_status()

    async def status(self, text: str) -> None:
        if self._status_step is None:
            self._status_step = cl.Step(
                name=text,
                type="run",
                parent_id=self._parent_id,
            )
            await self._status_step.send()
        else:
            self._status_step.name = text
            await self._status_step.update()

    # --- ошибки / фатальные ------------------------------------------

    async def tool_call_decode_failed(
        self,
        name: str,
        raw: str,
        error: str,
    ) -> None:
        await self._clear_status()
        step = cl.Step(name=name, type="tool", parent_id=self._parent_id)
        await step.send()
        await step.stream_token(raw, is_input=True)
        await _finalize_step(step, error, is_error=True)

    async def tool_execution_failed(
        self,
        call_id: str,
        error_kind: str,
        message: str,
    ) -> None:
        await self._clear_status()
        step = self._tool_steps_by_id.pop(call_id, None)
        if step is not None:
            await _finalize_step(
                step,
                f"[{error_kind}] {message}",
                is_error=True,
            )
            return
        await cl.Message(
            content=f"**tool failed** [{error_kind}]\n\n{message}",
            author="system",
            created_at=utc_now(),
        ).send()

    async def advisory(self, headline: str, body: str) -> None:
        await self._clear_status()
        await cl.Message(
            content=f"**{headline}**\n\n{body}",
            author="system",
            created_at=utc_now(),
        ).send()

    async def terminal(self, headline: str, body: str) -> None:
        await self._clear_status()
        await cl.Message(
            content=f"**{headline}**\n\n{body}",
            author="system",
        ).send()

    # --- диагностика -------------------------------------------------

    async def diagnostic(self, event: DiagnosticEvent) -> None:
        """Рендерит диагностику как сворачиваемый Step - только если включено.

        Структура step:
            name   - "diag: {topic}" (краткий заголовок для свёрнутого вида)
            input  - key=value по `details` (структурные данные)
            output - `body` если есть, иначе пусто
        """
        if not self._diagnostic:
            return
        step = cl.Step(
            name=f"diag: {event.topic}" if event.topic else "diag",
            type="run",
            parent_id=self._parent_id,
        )
        await step.send()
        if event.details:
            details_text = "\n".join(f"{k}={v}" for k, v in event.details.items() if v)
            if details_text:
                await step.stream_token(details_text, is_input=True)
        if event.body:
            await _finalize_step(step, event.body)

    # --- internals ---------------------------------------------------

    async def _open_answer(self) -> cl.Message:
        await self._clear_status()
        if self._answer_msg is None:
            # created_at фиксируем в момент Python-создания, чтобы chainlit
            # frontend сортировал answer строго ПОСЛЕ ранее созданных
            # cl.Step (которые ставят created_at в __init__).
            self._answer_msg = cl.Message(content="", created_at=utc_now())
            await self._answer_msg.send()
        return self._answer_msg

    async def _open_thinking(self) -> cl.Step:
        await self._clear_status()
        if self._thinking_step is None:
            self._thinking_step = cl.Step(
                name="thinking",
                type="run",
                parent_id=self._parent_id,
            )
            await self._thinking_step.send()
        return self._thinking_step

    async def _clear_status(self) -> None:
        if self._status_step is not None:
            await self._status_step.remove()
            self._status_step = None

    async def _drop_pending_answer(self) -> None:
        """Сбрасывает ссылку на не-завершённый answer; пустой удаляем.

        Why: AnswerDelta прилетает на ЛЮБОЙ первый delta.content, даже
        если итерация в итоге ушла в tool_calls без финального
        MessageEvent. Без сброса между итерациями новый
        DeltaEvent(ANSWER) будет писать в старое сообщение из
        прошлой итерации — оно остаётся выше последующих thinking/tool.
        How to apply: на IterationStarted; если answer_msg пустой —
        удаляем, чтобы фантомное сообщение не висело в чате.
        """
        if self._answer_msg is None:
            return
        if not (self._answer_msg.content or "").strip():
            await self._answer_msg.remove()
        self._answer_msg = None


async def _finalize_step(
    step: cl.Step,
    content: str,
    *,
    is_error: bool = False,
) -> None:
    """Финальный output step через streaming API: stream_token заменяет содержимое."""
    if is_error:
        step.is_error = True
    await step.stream_token(content, is_sequence=True)
