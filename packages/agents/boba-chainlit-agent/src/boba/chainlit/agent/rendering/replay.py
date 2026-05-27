"""Replay `HistoryService` событий в виде chainlit `StepDict`.

Источник правды для UI — сырые `AgentEvent` из `HistoryReader`. Маппинг
события на UI-операцию делает `AgentEventDispatcher`; нижеследующий
`StepDictTarget` реализует операции через накопление `StepDict`-ов
вместо вызовов chainlit-API.

`ContentDeltaEvent` в журнал не попадает (отфильтрован
`HistoryService.record`), и phase-события для replay визуально не нужны
— все streaming/phase-методы таргета no-op'ят. Replay видит только
финальные снапшоты + advisory/terminal.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import cast

from chainlit.step import StepDict

from boba.agent.events import DiagnosticEvent
from boba.agent.history import HistoryReader
from boba.chainlit.agent.models import ThreadId
from boba.chainlit.agent.rendering.dispatcher import (
    AgentEventDispatcher,
    EventRenderTarget,
)

__all__ = ["StepDictTarget", "replay_history_to_steps", "replay_history_to_steps_sync"]


async def replay_history_to_steps(
    history: HistoryReader,
    thread_id: ThreadId,
) -> list[StepDict]:
    """Прогнать журнал и вернуть chainlit-steps в порядке появления."""
    target = StepDictTarget(thread_id)
    dispatcher = AgentEventDispatcher(target)
    for event in history.events():
        await dispatcher.handle(event)
    return target.steps()


def replay_history_to_steps_sync(
    history: HistoryReader,
    thread_id: ThreadId,
) -> list[StepDict]:
    """Sync-обёртка для вызова из синхронного контекста (data_layer).

    Внутри dispatcher async, но реальных await'ов в `StepDictTarget` нет —
    `asyncio.run` тут безопасен и быстр.
    """
    return asyncio.run(replay_history_to_steps(history, thread_id))


class StepDictTarget(EventRenderTarget):
    """Реализует `EventRenderTarget` через накопление `StepDict`-ов.

    Все *_chunk/started/milestone/status методы — no-op'ы: replay не
    видит delta'ов и не показывает фазовые индикаторы.
    """

    def __init__(self, thread_id: ThreadId) -> None:
        self._thread_id = thread_id
        self._out: list[StepDict] = []
        # tool_call.id → индекс в _out для дозаписи output'а из tool_result.
        self._tool_index_by_call_id: dict[str, int] = {}

    def steps(self) -> list[StepDict]:
        return self._out

    # --- streaming (no-op: replay не видит delta'ов) -----------------

    async def answer_chunk(self, text: str) -> None:
        del text

    async def thinking_chunk(self, text: str) -> None:
        del text

    async def refusal_chunk(self, text: str) -> None:
        del text

    async def tool_args_chunk(self, call_id: str, text: str) -> None:
        del call_id, text

    # --- snapshot завершения -----------------------------------------

    async def user_query(self, text: str) -> None:
        self._append(type_="user_message", name="user", output=text)

    async def answer_complete(self, text: str) -> None:
        if not text:
            return
        self._append(type_="assistant_message", name="assistant", output=text)

    async def thinking_complete(self, text: str) -> None:
        self._append(type_="run", name="thinking", output=text)

    async def refusal_complete(self, text: str) -> None:
        if not text:
            return
        self._append(type_="assistant_message", name="refusal", output=text)

    async def tool_call_complete(
        self,
        call_id: str,
        name: str,
        args_json: str,
    ) -> None:
        self._tool_index_by_call_id[call_id] = len(self._out)
        self._append(type_="tool", name=name, output="", input_=args_json)

    async def tool_result(
        self,
        call_id: str,
        text: str,
        *,
        is_error: bool,
    ) -> None:
        idx = self._tool_index_by_call_id.get(call_id)
        if idx is None:
            # Orphan: result без предшествующего tool_call_complete —
            # отдельным tool-step'ом, чтобы не терять текст.
            self._append(
                type_="tool",
                name="tool_result",
                output=text,
                is_error=is_error,
            )
            return
        step = self._out[idx]
        step["output"] = text
        if is_error:
            step["isError"] = True

    async def feedback(self, text: str) -> None:
        self._append(
            type_="system_message",
            name="system",
            output=f"**Feedback to LLM**:\n\n{text}",
        )

    # --- phase-маркеры (no-op) ---------------------------------------

    async def iteration_started(self) -> None:
        return

    async def tool_execution_started(self, call_id: str) -> None:
        del call_id

    async def generation_milestone(self) -> None:
        return

    async def status(self, text: str) -> None:
        del text

    # --- ошибки / фатальные -----------------------------------------

    async def invalid_tool_call(
        self,
        name: str,
        raw_args: str,
        error: str,
    ) -> None:
        self._append(
            type_="tool",
            name=name,
            output=error,
            input_=raw_args,
            is_error=True,
        )

    async def tool_execution_failed(
        self,
        call_id: str,
        error_kind: str,
        message: str,
    ) -> None:
        text = f"[{error_kind}] {message}"
        idx = self._tool_index_by_call_id.get(call_id)
        if idx is None:
            self._append(
                type_="tool",
                name="tool_failed",
                output=text,
                is_error=True,
            )
            return
        step = self._out[idx]
        step["output"] = text
        step["isError"] = True

    async def advisory(self, headline: str, body: str) -> None:
        self._append(
            type_="system_message",
            name="system",
            output=f"**{headline}**\n\n{body}",
        )

    async def terminal(self, headline: str, body: str) -> None:
        self._append(
            type_="system_message",
            name="system",
            output=f"**{headline}**\n\n{body}",
        )

    # --- диагностика (no-op: history не пишет DiagnosticEvent) -------

    async def diagnostic(self, event: DiagnosticEvent) -> None:
        # Defensive no-op: HistoryService отфильтровывает категорию
        # DIAGNOSTIC, поэтому replay их физически не увидит. Метод
        # остаётся ради совместимости с EventRenderTarget-протоколом.
        del event

    # --- internals ---------------------------------------------------

    def _append(
        self,
        *,
        type_: str,
        name: str,
        output: str,
        input_: str = "",
        is_error: bool = False,
    ) -> None:
        step: dict[str, object] = {
            "id": _sid(),
            "threadId": self._thread_id,
            "parentId": None,
            "type": type_,
            "name": name,
            "input": input_,
            "output": output,
            "createdAt": _now(),
        }
        if is_error:
            step["isError"] = True
        self._out.append(cast("StepDict", step))


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sid() -> str:
    return str(uuid.uuid4())
