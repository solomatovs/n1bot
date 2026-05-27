"""
Единый источник правды для маппинга `AgentEvent` -> UI-операции
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, ClassVar, Protocol

from boba.agent.events import (
    AdvisoryEvent,
    AnswerDelta,
    AnswerMessage,
    ContentDeltaEvent,
    ContentSnapshotEvent,
    DiagnosticEvent,
    FeedbackToLLMAdded,
    IterationStarted,
    PhaseEvent,
    RefusalDelta,
    RefusalMessage,
    StreamKind,
    TerminalEvent,
    ThinkingDelta,
    ThinkingMessage,
    ToolCallDecodeFailedMessage,
    ToolCallDelta,
    ToolCallMessage,
    ToolExecutionFailed,
    ToolExecutionStarted,
    ToolPart,
    ToolResultReady,
    TotalMessage,
    UserQueryReceived,
)
from boba.llm.events import FinishReason
from boba.tools.domain import ErrorResult, JsonResult, TextResult, ToolResult

if TYPE_CHECKING:
    from boba.agent.events import AgentEvent

__all__ = ["AgentEventDispatcher", "EventRenderTarget"]


class EventRenderTarget(Protocol):
    """Адаптер UI-операций. Реализация — chainlit-live или replay-StepDict."""

    # streaming (живой токен-стрим, replay no-op)

    async def answer_chunk(self, text: str) -> None: ...
    async def thinking_chunk(self, text: str) -> None: ...
    async def refusal_chunk(self, text: str) -> None: ...
    async def tool_args_chunk(self, call_id: str, text: str) -> None: ...

    # завершения content-снапшотов

    async def user_query(self, text: str) -> None: ...
    async def answer_complete(self, text: str) -> None: ...
    async def thinking_complete(self, text: str) -> None: ...
    async def refusal_complete(self, text: str) -> None: ...
    async def tool_call_complete(
        self,
        call_id: str,
        name: str,
        args_json: str,
    ) -> None: ...
    async def tool_result(
        self,
        call_id: str,
        text: str,
        *,
        is_error: bool,
    ) -> None: ...
    async def feedback(self, text: str) -> None: ...

    # phase-маркеры (live UX, replay no-op)

    async def iteration_started(self) -> None: ...
    async def tool_execution_started(self, call_id: str) -> None: ...
    async def generation_milestone(self) -> None: ...
    async def status(self, text: str) -> None: ...

    # ошибки и фатальные события

    async def tool_call_decode_failed(
        self,
        name: str,
        raw: str,
        error: str,
    ) -> None: ...
    async def tool_execution_failed(
        self,
        call_id: str,
        error_kind: str,
        message: str,
    ) -> None: ...
    async def advisory(self, headline: str, body: str) -> None: ...
    async def terminal(self, headline: str, body: str) -> None: ...

    # диагностика (telemetry; видна только при diagnostic-mode)

    async def diagnostic(self, event: DiagnosticEvent) -> None: ...


class AgentEventDispatcher:
    """Маппит каждый `AgentEvent` на ровно один вызов `EventRenderTarget`.

    Семантика категорий событий:
        ContentDeltaEvent  — стриминг токенов (`*_chunk`)
        ContentSnapshotEvent — завершённый блок контента (`*_complete`,
                               `user_query`, `tool_call_complete`,
                               `tool_result`, `feedback`)
        PhaseEvent         — состояния процесса (`iteration_started`,
                               `tool_execution_started`,
                               `generation_milestone`, `status`)
        AdvisoryEvent      — нефатальные ошибки (`tool_call_decode_failed`,
                               `tool_execution_failed`, `advisory`)
        TerminalEvent      — фатальное завершение (`terminal`)
        DiagnosticEvent    — телеметрия (`diagnostic`); решение «показывать
                               или нет» принимает target по своему toggle
    """

    _NS_PER_US: ClassVar[int] = 1_000
    _NS_PER_MS: ClassVar[int] = 1_000_000
    _NS_PER_S: ClassVar[int] = 1_000_000_000

    def __init__(self, target: EventRenderTarget) -> None:
        self._target = target

    async def handle(self, event: AgentEvent) -> None:  # noqa: C901, PLR0912
        # Порядок case'ов: сначала конкретные delta/snapshot/phase события,
        # потом fall-through на категории. У pydantic-discriminated unions
        # тип проверяется по полю `type`, поэтому конкретный case
        # сматчится раньше базового.
        match event:
            # ContentDelta
            case AnswerDelta():
                if event.chunk:
                    await self._target.answer_chunk(event.chunk)
            case ThinkingDelta():
                if event.chunk:
                    await self._target.thinking_chunk(event.chunk)
            case RefusalDelta():
                if event.chunk:
                    await self._target.refusal_chunk(event.chunk)
            case ToolCallDelta():
                if event.chunk:
                    await self._target.tool_args_chunk(
                        event.stream_id,
                        event.chunk,
                    )

            # --- ContentSnapshot --------------------------------------
            case UserQueryReceived(query=q):
                await self._target.user_query(q)
            case AnswerMessage(content=c):
                await self._target.answer_complete(c)
            case ThinkingMessage(content=c):
                await self._target.thinking_complete(c)
            case RefusalMessage(content=c):
                await self._target.refusal_complete(c)
            case ToolCallMessage(call=call):
                await self._target.tool_call_complete(
                    call.id,
                    call.name,
                    call.args_json(),
                )
            case ToolResultReady(call=call, result=result):
                await self._target.tool_result(
                    call.id,
                    _result_text(result.result),
                    is_error=False,
                )
            case FeedbackToLLMAdded(content=c):
                await self._target.feedback(c)

            # PhaseEvent
            case IterationStarted():
                await self._target.iteration_started()
            case ToolExecutionStarted(tool_call_id=tid):
                await self._target.tool_execution_started(tid)
            case TotalMessage():
                # терминатор генерации: гасим статус + подсвечиваем исход
                await self._target.generation_milestone()
                await self._handle_generation_completed(event)

            # AdvisoryEvent
            case ToolCallDecodeFailedMessage(failure=failure):
                await self._target.tool_call_decode_failed(
                    failure.name,
                    failure.raw,
                    failure.error,
                )
            case ToolExecutionFailed(call=call, failure=failure):
                await self._target.tool_execution_failed(
                    call.id,
                    failure.error_kind,
                    failure.message,
                )

            # DiagnosticEvent
            case DiagnosticEvent() as diagnostic_evt:
                # Target сам решает, показывать ли (по своему toggle).
                # Дополнительной специализации по type не делаем -
                # target фильтрует по `topic`.
                await self._target.diagnostic(diagnostic_evt)

            # Generic fall-throughs
            case ContentSnapshotEvent() as snapshot:
                # Прочие snapshot'ы (например, USER_QUERY с не-UserQueryReceived
                # или TOOL_INVOCATION/RESULT без ToolResultReady) — оставляем
                # последним fallback'ом, чтобы не потерять текст.
                await self._handle_unmatched_snapshot(snapshot)
            case PhaseEvent() as phase:
                await self._target.status(self._render_phase_text(phase))
            case AdvisoryEvent() as advisory:
                await self._target.advisory(
                    advisory.headline,
                    advisory.body or "",
                )
            case TerminalEvent() as terminal:
                await self._target.terminal(
                    terminal.headline,
                    terminal.body or "",
                )
            case ContentDeltaEvent():
                # ContentDelta без специализации (StreamKind, который мы
                # не выделили выше) — игнорируем.
                return

    async def _handle_generation_completed(self, event: TotalMessage) -> None:
        """Подсветить «не-нормальный» исход генерации в UI.

        Для STOP / TOOL_CALLS — тихий no-op: per-field *Message события
        уже отрисовали контент, и `TotalMessage` несёт лишь агрегат,
        который без аномалии не нуждается в отдельной отрисовке.

        Для LENGTH — system-advisory: ответ обрезан, пользователь должен
        об этом узнать. Для CONTENT_FILTER — terminal: ответ заблокирован
        фильтром провайдера, повторение с тем же промптом обычно
        бесполезно.

        Тексты — на русском, как и весь UI Chainlit-агента; severity
        события уже выставлен по finish_reason в `_derive`.
        """
        match event.finish_reason:
            case FinishReason.LENGTH:
                body = self._fmt_outcome_body(event)
                await self._target.advisory(
                    "Ответ обрезан: достигнут лимит токенов",
                    body,
                )
            case FinishReason.CONTENT_FILTER:
                body = self._fmt_outcome_body(event)
                await self._target.terminal(
                    "Ответ заблокирован фильтром провайдера",
                    body,
                )
            case FinishReason.STOP | FinishReason.TOOL_CALLS:
                # Нормальный путь — контент уже отрисован per-field событиями.
                return

    @staticmethod
    def _fmt_outcome_body(event: TotalMessage) -> str:
        msg = event.message
        parts: list[str] = [f"finish_reason: {event.finish_reason.value}"]
        if msg.content:
            parts.append(f"answer length: {len(msg.content)} chars")
        if msg.thinking:
            parts.append(f"thinking length: {len(msg.thinking)} chars")
        if msg.tool_calls:
            parts.append(f"tool_calls: {len(msg.tool_calls)}")
        if msg.tool_call_decode_failures:
            n = len(msg.tool_call_decode_failures)
            parts.append(f"tool_call_decode_failures: {n}")
        return "\n".join(parts)

    async def _handle_unmatched_snapshot(
        self,
        event: ContentSnapshotEvent,
    ) -> None:
        # На случай новых ContentSnapshotEvent'ов, добавленных позже —
        # маршрутизируем по stream_kind / part, чтобы не молчать.
        if event.stream_kind == StreamKind.TOOL_INVOCATION:
            if event.part == ToolPart.RESULT and event.stream_id:
                await self._target.tool_result(
                    event.stream_id,
                    event.body,
                    is_error=False,
                )
            return
        if event.stream_kind == StreamKind.FEEDBACK:
            await self._target.feedback(event.body)
            return
        # Остальные «неизвестные» снапшоты — best-effort через advisory,
        # чтобы текст не терялся в UI.
        if event.body:
            await self._target.advisory(
                event.headline or "snapshot",
                event.body,
            )

    @classmethod
    def _render_phase_text(cls, e: PhaseEvent) -> str:
        parts: list[str] = []
        if e.details:
            details = " ".join(f"{k}={v}" for k, v in e.details.items() if v)
            if details:
                parts.append(details)
        duration = cls._fmt_duration(e.duration_ns)
        if duration:
            parts.append(duration)
        if not parts:
            return e.label
        return f"{e.label} ({', '.join(parts)})"

    @classmethod
    def _fmt_duration(cls, ns: int) -> str:
        if ns <= 0:
            return ""
        if ns < cls._NS_PER_MS:
            return f"+{ns / cls._NS_PER_US:.0f}µs"
        if ns < cls._NS_PER_S:
            return f"+{ns / cls._NS_PER_MS:.0f}ms"
        return f"+{ns / cls._NS_PER_S:.2f}s"


def _result_text(result: ToolResult) -> str:
    # Chainlit Step рендерит output как markdown. Tools часто кладут
    # markdown-таблицы / форматированный текст внутрь TextResult и
    # JsonResult.payload — поэтому здесь возвращаем markdown-friendly
    # представление, а не raw json.dumps (который ломал переносы строк
    # на \n и не давал таблицам отрисоваться).
    match result:
        case TextResult(text=t):
            return t
        case JsonResult(payload=p):
            pretty = json.dumps(p, ensure_ascii=False, indent=2)
            # Однострочный JSON (пустые [], {}, null, скаляры) Chainlit/markdown
            # ломает в fenced-блоке: верхняя пара ``` "съедается", видна только
            # нижняя. Inline-code рендерится корректно.
            if "\n" not in pretty:
                return f"`{pretty}`"
            return f"```json\n{pretty}\n```"
        case ErrorResult(message=m):
            if "\n" in m:
                return f"**Error:**\n\n{m}"
            return f"**Error:** {m}"
