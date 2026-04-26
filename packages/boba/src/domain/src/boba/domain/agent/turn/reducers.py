"""Базовые reducer'ы для :class:`TurnSpec`.

По одному reducer'у на ось :class:`TurnState`. Каждый изолирован:
смотрит только на свою ось, не знает о других. Политики уровня
"temp ниже при feedback" / "fallback-модель при retry" / "другой
system prompt при каком-то теге" добавляются как отдельные
reducer'ы с более высоким ``priority``, мутирующие те же slot'ы
поверх базовых.

Все reducer'ы переиспользуют существующие доменные сервисы —
:class:`PromptFactory`, :class:`ToolsService`, :class:`MessageService`
— никакой логики сборки не дублируется.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from boba.domain.agent.prompt import PromptFactory, PromptProvider
from boba.domain.agent.turn.spec import TurnResolveContext, TurnState
from boba.domain.core.patterns import ContextPrioritySource, StrId
from boba.domain.core.tools import Tool, ToolsService
from boba.domain.llm.models import (
    LLMMessage,
    LLMToolRequest,
    LLMToolSchema,
)

_MODEL_ID = StrId("model")
_SYSTEM_ID = StrId("system")
_HISTORY_ID = StrId("history")
_TOOLS_ID = StrId("tools")
_SAMPLING_ID = StrId("sampling")


def _tool_to_schema(tool: Tool[Any]) -> LLMToolSchema:
    """Конверсия доменного :class:`Tool` в data-only :class:`LLMToolSchema`.

    Граница между tools-доменом и LLM-слоем: дальше в LLM едет только
    name + description + JSON-schema, без execute-логики и валидаторов.
    ``Tool.definition`` возвращает :class:`ObjectSchema` напрямую —
    тот же примитив, что описывает config-секцию. Текстовое описание
    действия tool'а лежит на ``schema.description``.
    """
    schema = tool.definition()
    wire = schema.build_wire_schema()
    return LLMToolSchema(
        name=tool.tool_id().to_wire(),
        description=schema.description,
        parameters_schema={
            "type": "object",
            "properties": wire.properties,
            "required": wire.required,
        },
    )


class ModelReducer(ContextPrioritySource[TurnResolveContext, StrId, TurnState]):
    """Берёт модель из ``ctx.agent.agent_request.model``.

    Для override'а (fallback на другую модель по тегу trigger'а)
    регистрируй отдельный reducer с более высоким priority: он
    получит уже заполненный slot и может заменить при условии.
    """

    def __init__(self, priority: int = 10) -> None:
        self._priority = priority

    def id(self) -> StrId:
        return _MODEL_ID

    def priority(self) -> int:
        return self._priority

    def apply(self, ctx: TurnResolveContext, state: TurnState) -> TurnState:
        state.model = ctx.agent.agent_request.model
        return state


class SystemPromptReducer(ContextPrioritySource[TurnResolveContext, StrId, TurnState]):
    """Собирает system-prompt через :class:`PromptFactory` каждую итерацию.

    Переиспользует существующих :class:`PromptProvider`'ов:
    содержимое пересобирается per-call, так что провайдеры могут
    реагировать на ``ctx.agent`` (workspace, iteration, etc.).
    Пустой результат (провайдеры ничего не вернули) →
    ``state.system_message`` остаётся ``None``, finalize упадёт с
    :class:`LLMRequestSystemMessageNoneError`.
    """

    def __init__(
        self,
        providers: Sequence[PromptProvider],
        priority: int = 20,
    ) -> None:
        self._providers = providers
        self._priority = priority

    def id(self) -> StrId:
        return _SYSTEM_ID

    def priority(self) -> int:
        return self._priority

    def apply(self, ctx: TurnResolveContext, state: TurnState) -> TurnState:
        content = (
            PromptFactory(ctx.agent, self._providers)
            .build()
            .to_string()
        )
        if content:
            state.system_message = LLMMessage(role="system", content=content)
        return state


class HistoryReducer(ContextPrioritySource[TurnResolveContext, StrId, TurnState]):
    """Копирует весь диалог из :class:`MessageService` в state.

    Все записи (user-query, assistant, tool_result, feedback) уже
    зафиксированы в svc через :class:`DialogueWriter` к моменту
    вызова reducer'а — снапшот полный и свежий.
    """

    def __init__(self, priority: int = 30) -> None:
        self._priority = priority

    def id(self) -> StrId:
        return _HISTORY_ID

    def priority(self) -> int:
        return self._priority

    def apply(self, ctx: TurnResolveContext, state: TurnState) -> TurnState:
        state.messages = tuple(ctx.message_service.message_iter())
        return state


class ToolsReducer(ContextPrioritySource[TurnResolveContext, StrId, TurnState]):
    """Каталог tools из :class:`ToolsService`.

    Ничего не знает о конкретных тулах — просто подхватывает то,
    что сервис отдаёт на момент вызова. Подмножества/фильтры (например,
    "в режиме feedback прячем destructive-тулы") — отдельный reducer
    поверх с более высоким priority.
    """

    def __init__(
        self,
        tools_service: ToolsService,
        parallel_tool_calls: bool = True,
        priority: int = 40,
    ) -> None:
        self._tools_service = tools_service
        self._parallel = parallel_tool_calls
        self._priority = priority

    def id(self) -> StrId:
        return _TOOLS_ID

    def priority(self) -> int:
        return self._priority

    def apply(self, ctx: TurnResolveContext, state: TurnState) -> TurnState:
        state.tools = LLMToolRequest(
            tools=tuple(_tool_to_schema(t) for t in self._tools_service.tools()),
            parallel_tool_calls=self._parallel,
        )
        return state


class AgentRequestSamplingReducer(
    ContextPrioritySource[TurnResolveContext, StrId, TurnState]
):
    """Берёт ``SamplingParams`` из ``ctx.agent.agent_request.sampling``.

    Семантика «передано — применяем, не передано — не трогаем»:
    ``None`` в ``agent_request.sampling`` оставляет ``state.sampling``
    как есть (дефолтный :class:`SamplingParams` со всеми ``None``-полями
    — провайдер ничего не подложит, см.
    :meth:`~boba.adapters.llm.openai_request.\
ToOpenAIRequestConverter._apply_sampling`).

    Глобального дефолта sampling в системе нет: единственный источник —
    :class:`~boba.domain.agent.models.AgentRequest`. Для per-trigger
    тюнинга (ниже temperature при tag="feedback") — отдельный
    TagAwareSamplingReducer с большим priority, который читает
    ``ctx.trigger.tags`` и патчит ``state.sampling`` уже поверх
    выставленного здесь значения.
    """

    def __init__(self, priority: int = 50) -> None:
        self._priority = priority

    def id(self) -> StrId:
        return _SAMPLING_ID

    def priority(self) -> int:
        return self._priority

    def apply(self, ctx: TurnResolveContext, state: TurnState) -> TurnState:
        sampling = ctx.agent.agent_request.sampling
        if sampling is not None:
            state.sampling = sampling
        return state
