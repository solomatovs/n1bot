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

from boba.domain.agent.prompt import PromptFactory, PromptKind, PromptProvider
from boba.domain.agent.turn.spec import TurnResolveContext, TurnState
from boba.domain.core.patterns import ContextPrioritySource, StrId
from boba.domain.core.tools import ToolsService
from boba.domain.llm.models import LLMMessage, LLMToolRequest, SamplingParams

_MODEL_ID = StrId("model")
_SYSTEM_ID = StrId("system")
_HISTORY_ID = StrId("history")
_TOOLS_ID = StrId("tools")
_SAMPLING_ID = StrId("sampling")


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
            .to_string(PromptKind.SYSTEM)
        )
        if content:
            state.system_message = LLMMessage(role="system", content=content)
        return state


class HistoryReducer(ContextPrioritySource[TurnResolveContext, StrId, TurnState]):
    """Копирует весь диалог из :class:`MessageService` в state.

    Ставится ПОСЛЕ :meth:`TurnSpec.initial` (где effects уже
    применены к сервису). Если между initial и этим reducer'ом
    никто не дописывает в svc — снапшот полный и свежий.
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
            tools=tuple(self._tools_service.tools()),
            parallel_tool_calls=self._parallel,
        )
        return state


class SamplingReducer(ContextPrioritySource[TurnResolveContext, StrId, TurnState]):
    """Константные sampling-параметры из конфига.

    Для per-trigger тюнинга (ниже temperature при tag="feedback")
    — отдельный TagAwareSamplingReducer с большим priority, который
    читает ``ctx.trigger.tags`` и патчит ``state.sampling``.
    """

    def __init__(
        self,
        sampling: SamplingParams,
        priority: int = 50,
    ) -> None:
        self._sampling = sampling
        self._priority = priority

    def id(self) -> StrId:
        return _SAMPLING_ID

    def priority(self) -> int:
        return self._priority

    def apply(self, ctx: TurnResolveContext, state: TurnState) -> TurnState:
        state.sampling = self._sampling
        return state
