"""
Replay `HistoryService` событий в виде chainlit `StepDict`
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, NamedTuple, cast

from boba.agent.events import DiagnosticEvent
from boba.agent.history import HistoryReader
from boba.chainlit.models import ThreadId
from boba.chainlit.rendering.chart_figure import build_plotly_data_uri
from boba.chainlit.rendering.dispatcher import (
    AgentEventDispatcher,
    EventRenderTarget,
)
from chainlit.element import ElementDict
from chainlit.step import StepDict

__all__ = [
    "StepDictTarget",
    "ThreadContent",
    "replay_history_to_thread",
    "replay_history_to_thread_sync",
]

_logger = logging.getLogger(__name__)


class ThreadContent(NamedTuple):
    """Восстановленная из журнала лента треда: шаги + элементы (графики)."""

    steps: list[StepDict]
    elements: list[ElementDict]


async def replay_history_to_thread(
    history: HistoryReader,
    thread_id: ThreadId,
) -> ThreadContent:
    """Прогнать журнал и вернуть chainlit steps + elements в порядке появления."""
    target = StepDictTarget(thread_id)
    dispatcher = AgentEventDispatcher(target)
    for event in history.events():
        await dispatcher.handle(event)
    return ThreadContent(target.steps(), target.elements())


def replay_history_to_thread_sync(
    history: HistoryReader,
    thread_id: ThreadId,
) -> ThreadContent:
    """Sync-обёртка для вызова из синхронного контекста (data_layer).

    Внутри dispatcher async, но реальных await'ов в `StepDictTarget` нет —
    `asyncio.run` тут безопасен и быстр.
    """
    return asyncio.run(replay_history_to_thread(history, thread_id))


class StepDictTarget(EventRenderTarget):
    """Реализует `EventRenderTarget` через накопление `StepDict`-ов.

    Все *_chunk/started/milestone/status методы — no-op'ы: replay не
    видит delta'ов и не показывает фазовые индикаторы.
    """

    def __init__(self, thread_id: ThreadId) -> None:
        self._thread_id = thread_id
        self._out: list[StepDict] = []
        self._elements_out: list[ElementDict] = []
        # tool_call.id → индекс в _out для дозаписи output'а из tool_result.
        self._tool_index_by_call_id: dict[str, int] = {}

    def steps(self) -> list[StepDict]:
        return self._out

    def elements(self) -> list[ElementDict]:
        return self._elements_out

    # --- streaming (no-op: replay не видит delta'ов) -----------------

    async def answer_chunk(self, text: str) -> None:
        del text

    async def thinking_chunk(self, text: str) -> None:
        del text

    async def refusal_chunk(self, text: str) -> None:
        del text

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

    async def tool_result(
        self,
        call_id: str,
        text: str,
        *,
        is_error: bool,
    ) -> None:
        idx = self._tool_index_by_call_id.get(call_id)
        if idx is None:
            # Orphan: result без предшествующего tool_started —
            # отдельным tool-step'ом, чтобы не терять текст.
            self._append(
                type_="tool",
                name="tool_result",
                output=text,
                is_error=is_error,
            )
            return
        step = self._out[idx]
        step["output"] = _close_fence(text)
        if is_error:
            step["isError"] = True

    async def tool_chart(
        self,
        call_id: str,
        spec: Mapping[str, Any],
        title: str | None,
    ) -> None:
        # Симметрично live: tool-step закрываем текстом, а сам график рисуем
        # видимым сообщением-контейнером с plotly-элементом. Содержимое графика
        # восстанавливается из журнала (spec) и встраивается в `url` как
        # data:-URI — постоянного файлового хранилища у нас нет.
        text = f"график отрисован: {title}" if title else "график отрисован"
        idx = self._tool_index_by_call_id.get(call_id)
        if idx is None:
            self._append(type_="tool", name="chart", output=text)
        else:
            self._out[idx]["output"] = text

        try:
            url = build_plotly_data_uri(spec)
        except Exception:
            # Битый spec в истории не должен ронять загрузку всего треда.
            _logger.exception("replay: не удалось восстановить график из истории")
            return
        container_id = self._append(
            type_="assistant_message",
            name="chart",
            output=title or "",
        )
        self._elements_out.append(
            cast(
                "ElementDict",
                {
                    "id": _sid(),
                    "threadId": self._thread_id,
                    "type": "plotly",
                    "name": title or "chart",
                    "display": "inline",
                    "size": "medium",
                    "url": url,
                    "mime": "application/json",
                    "forId": container_id,
                },
            ),
        )

    async def feedback(self, text: str) -> None:
        self._append(
            type_="system_message",
            name="system",
            output=f"**Feedback to LLM**:\n\n{text}",
        )

    # --- phase-маркеры ----------------------------------------------

    async def iteration_started(self) -> None:
        return

    async def tool_started(
        self,
        call_id: str,
        name: str,
        args_json: str,
    ) -> None:
        # Открываем tool-step именно здесь: ToolCallMessage в UI не рисуем,
        # `ToolExecutionStarted` — единственный источник для создания step'а.
        self._tool_index_by_call_id[call_id] = len(self._out)
        self._append(type_="tool", name=name, output="", input_=args_json)

    async def generation_milestone(self) -> None:
        return

    async def status(self, text: str) -> None:
        del text

    # --- ошибки / фатальные -----------------------------------------

    async def tool_call_decode_failed(
        self,
        name: str,
        raw: str,
        error: str,
    ) -> None:
        self._append(
            type_="tool",
            name=name,
            output=error,
            input_=raw,
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
    ) -> str:
        """Добавить StepDict; вернуть его id (для привязки `ElementDict.forId`)."""
        step_id = _sid()
        step: dict[str, object] = {
            "id": step_id,
            "threadId": self._thread_id,
            "parentId": None,
            "type": type_,
            "name": name,
            "input": input_,
            "output": _close_fence(output),
            "createdAt": _now(),
        }
        if is_error:
            step["isError"] = True
        self._out.append(cast("StepDict", step))
        return step_id


def _close_fence(text: str) -> str:
    """Добивает завершающий '\\n': без него последний code-fence упирается в
    EOF и react-markdown Chainlit не финализирует fence — три бэктика
    протекают в UI как текст."""
    if text and not text.endswith("\n"):
        return f"{text}\n"
    return text


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sid() -> str:
    return str(uuid.uuid4())
