"""Агент: упакованный ход модели с инструментами для любого подпроекта.

Агент собирается один раз из GraphSpec — модель, инструменты, системный промпт,
хранилище истории, слои — и дальше отвечает на вопросы: `ask(thread, question)` ведёт
один ход, в котором модель зовёт инструменты, пока ей это нужно, и возвращает итог
вместе с протоколом хода — всем, что попало в окно контекста.

Инструменты агент получает готовыми BaseTool и сам их не оборачивает: сервис собирает
их ToolSet'ом через общий конвейер (запускатель, конфиг, журнал, ошибки в результат),
чат берёт из своего реестра. Контекст вызова агенту не нужен; тому, кто работает от
имени пользователя, его открывает вызывающий снаружи, как делает чат.

История живёт в checkpointer по thread id: память процесса для пакетных задач, postgres
для чата; вопросы в одном thread видят предыдущие, ответ хода отдаёт только новые
сообщения. `forget(thread)` стирает thread из хранилища.

Ошибки:
AgentError — модель недоступна или ответила некорректно, предел обращений к модели за
    ход исчерпан, граф хода не сошёлся, ход закончился не ответом модели.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar

from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphRecursionError
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, ConfigDict, Field

from boba.agent.graph import AgentGraphBuilder, GraphSpec

__all__ = ["Agent", "AgentError", "AgentLimits", "AgentReply"]


class AgentError(Exception):
    """Ход агента не дал ответа."""


class AgentLimits(BaseModel):
    """Пределы одного хода."""

    model_config = ConfigDict(frozen=True)

    max_model_calls: int = Field(gt=0)
    """Сколько раз за один вопрос можно обратиться к модели; больше — ошибка хода."""


class AgentReply(BaseModel):
    """Итог хода: ответ модели и всё, что за этот ход попало в окно контекста."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    text: str
    messages: Sequence[BaseMessage]
    """Сообщения хода: вопрос, ответы модели с вызовами, результаты инструментов."""
    model_calls: int
    tool_calls: int


class TurnMessages:
    """Разбор сообщений хода: ответ и счётчики."""

    @classmethod
    def reply(cls, thread: str, messages: Sequence[BaseMessage]) -> AgentReply:
        if not messages:
            raise AgentError(f"agent thread {thread!r}: the turn produced no messages")

        last = messages[-1]
        if not isinstance(last, AIMessage):
            raise AgentError(
                f"agent thread {thread!r}: expected the turn to end with a model "
                f"reply, got {type(last).__name__}"
            )

        return AgentReply(
            text=last.text,
            messages=list(messages),
            model_calls=cls._model_calls(messages),
            tool_calls=cls._tool_calls(messages),
        )

    @staticmethod
    def _model_calls(messages: Sequence[BaseMessage]) -> int:
        count = 0
        for message in messages:
            if isinstance(message, AIMessage):
                count += 1

        return count

    @staticmethod
    def _tool_calls(messages: Sequence[BaseMessage]) -> int:
        count = 0
        for message in messages:
            if isinstance(message, AIMessage):
                count += len(message.tool_calls)

        return count


class Agent:
    """Модель с инструментами и промптом, готовая отвечать на вопросы.

    Предел обращений к модели встаёт слоем перед моделью. Один экземпляр обслуживает
    много thread'ов, история каждого лежит в checkpointer из spec.
    """

    THREAD_KEY: ClassVar[str] = "thread_id"
    NODES_PER_CALL: ClassVar[int] = 2
    """Узлов графа на одно обращение к модели без слоёв: модель и её инструменты."""
    NODES_PER_LAYER: ClassVar[int] = 2
    """Узлов графа, которые добавляет один слой вокруг обращения: до и после модели."""

    def __init__(
        self, spec: GraphSpec, limits: AgentLimits, builder: AgentGraphBuilder
    ) -> None:
        limit = ModelCallLimitMiddleware(
            run_limit=limits.max_model_calls, exit_behavior="error"
        )
        bounded = GraphSpec(
            chat=spec.chat,
            tools=spec.tools,
            system_prompt=spec.system_prompt,
            checkpointer=spec.checkpointer,
            middleware=[limit, *spec.middleware],
        )
        self._spec = bounded
        self._limits = limits
        self._graph: CompiledStateGraph = builder.build(bounded)

    def tool_names(self) -> tuple[str, ...]:
        names: list[str] = []
        for tool in self._spec.tools:
            names.append(tool.name)

        return tuple(names)

    async def ask(self, thread: str, question: str) -> AgentReply:
        """Один ход в thread: вопрос, вызовы инструментов, ответ."""
        config = self._config(thread)
        before = await self._length(config)

        try:
            state = await self._graph.ainvoke(
                {"messages": [HumanMessage(content=question)]}, config=config
            )
        except ModelCallLimitExceededError as exc:
            raise AgentError(
                f"agent thread {thread!r}: model call limit "
                f"{self._limits.max_model_calls} reached without a final reply"
            ) from exc
        except GraphRecursionError as exc:
            raise AgentError(
                f"agent thread {thread!r}: the turn graph did not converge: {exc}"
            ) from exc
        except Exception as exc:
            raise AgentError(f"agent thread {thread!r}: turn failed: {exc}") from exc

        messages = list(state["messages"])

        return TurnMessages.reply(thread, messages[before:])

    async def forget(self, thread: str) -> None:
        """Стереть историю thread из хранилища."""
        await self._spec.checkpointer.adelete_thread(thread)

    def _config(self, thread: str) -> RunnableConfig:
        """Предел рекурсии графа — запасной: считается так, чтобы первым срабатывал
        предел обращений к модели, а не он."""
        layers = len(self._spec.middleware) * self.NODES_PER_LAYER
        per_call = self.NODES_PER_CALL + layers
        steps = (self._limits.max_model_calls + 1) * per_call

        return RunnableConfig(
            configurable={self.THREAD_KEY: thread}, recursion_limit=steps
        )

    async def _length(self, config: RunnableConfig) -> int:
        """Сколько сообщений в thread до хода: ответ отдаёт только новые."""
        snapshot = await self._graph.aget_state(config)
        values: dict[str, Any] = dict(snapshot.values)
        messages = values.get("messages")
        if messages is None:
            return 0

        return len(messages)
