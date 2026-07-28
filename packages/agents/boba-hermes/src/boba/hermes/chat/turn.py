import logging

import chainlit as cl

from boba.hermes.agent.api import HermesApiClient
from boba.hermes.agent.events import (
    HermesAssistantCompleted,
    HermesAssistantDelta,
    HermesErrorEvent,
    HermesEvent,
    HermesEventName,
    HermesToolEvent,
)
from boba.hermes.errors import AgentError

logger = logging.getLogger(__name__)


class HermesTurn:
    """Ход агента в chainlit: события hermes становятся сообщением и шагами.

    Сам ход и его историю ведёт hermes, поэтому здесь только отрисовка:
    в data layer create_step ничего не пишет, шаги живут до конца сессии
    в браузере и перечитываются из hermes при открытии треда.
    """

    def __init__(self, client: HermesApiClient) -> None:
        self._client = client
        self._answer = cl.Message(content="")
        # вызовов одного инструмента за ход бывает несколько; закрываем их
        # в порядке открытия, других связок tool.started с tool.completed
        # api_server не даёт
        self._steps: dict[str, list[cl.Step]] = {}

    async def run(self, session_id: str, message: str) -> None:
        """Отправить сообщение в сессию и показать ход до его конца."""
        # chat/stream работает только с заведённой сессией, а тред chainlit
        # мог быть создан до того, как появилась связка с профилем
        await self._client.create_session(session_id, source="api_server")

        async for event in self._client.chat_stream(session_id, message):
            await self._apply(event)

        await self._answer.send()

    async def _apply(self, event: HermesEvent) -> None:
        """Событие потока в изменение того, что видит пользователь."""
        if isinstance(event, HermesAssistantDelta):
            await self._answer.stream_token(event.delta)

        elif isinstance(event, HermesToolEvent):
            await self._tool(event)

        elif isinstance(event, HermesAssistantCompleted):
            self._completed(event)

        elif isinstance(event, HermesErrorEvent):
            raise AgentError(
                internal_detail=f"hermes run failed: {event.message}",
                user_detail="Agent run failed",
            )

    async def _tool(self, event: HermesToolEvent) -> None:
        """Вызов инструмента: tool.started открывает шаг, остальные закрывают."""
        if event.name == HermesEventName.TOOL_STARTED:
            await self._start_step(event)
        else:
            await self._finish_step(event)

    async def _start_step(self, event: HermesToolEvent) -> None:
        step = cl.Step(name=self._tool_name(event), type="tool")
        step.input = event.preview or ""
        await step.send()
        self._steps.setdefault(self._tool_name(event), []).append(step)

    async def _finish_step(self, event: HermesToolEvent) -> None:
        opened = self._steps.get(self._tool_name(event)) or []
        if not opened:
            logger.warning("hermes %s without a started step", event.name)
            return

        step = opened.pop(0)
        # результат вызова в событии не приходит, он остаётся в истории сессии
        step.is_error = event.name == HermesEventName.TOOL_FAILED
        await step.update()

    def _completed(self, event: HermesAssistantCompleted) -> None:
        """Ответ целиком; дельт может не быть, если провайдер их не шлёт."""
        if not self._answer.content:
            self._answer.content = event.content

    @staticmethod
    def _tool_name(event: HermesToolEvent) -> str:
        return event.tool_name or "tool"
