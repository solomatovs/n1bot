"""Инструмент хоста и его вызов: исполняемая сторона порта инструментов.

Автор объявляет тело декоратором `@tool` фасада (PayloadTool); здесь оно
становится исполняемым объектом хоста HostedTool — имя, описание, схема
аргументов и одно async-тело, на которое ложатся обвязки. Тело получает
вызов объектом HostedCall (инструмент, идентификатор вызова, аргументы) и
отдаёт результат семейства ToolResultBase. Вызов инструмента — узел DagNode
(идентификатор вызова, имя, аргументы), итог — NodeOutcome; клиенту порта
инструмент виден карточкой ToolCard с JSON-схемой вызова, её строит
ToolSchema. Типы контракта порта лежат в boba.toolkit.dag; здесь его
реализации: DirectCalls — путь вызова узла (NodeCalls) прямым вызовом тела.

Ошибки:
TypeError — тело инструмента хоста вернуло не результат семейства
    ToolResultBase.
ToolFacadeError — у объявления фасада нет тела либо тело автора вернуло не
    модель результата.
pydantic.ValidationError — аргументы вызова не проходят схему инструмента.
"""

from __future__ import annotations

import textwrap
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import partial
from typing import Any, TypeAlias

from pydantic import BaseModel, ConfigDict, create_model
from pydantic.fields import FieldInfo

from boba.toolkit.calls import CallViews, FieldView
from boba.toolkit.dag import (
    DagNode,
    NodeBody,
    NodeCalls,
    NodeOutcome,
    NodeOutcomes,
    ToolCard,
)
from boba.toolkit.entry import ToolAddress
from boba.toolkit.facade import PayloadTool
from boba.toolkit.result import ToolResultBase

__all__ = [
    "AsyncCall",
    "DirectCalls",
    "HostedCall",
    "HostedTool",
    "ToolHosting",
    "ToolSchema",
]


@dataclass(frozen=True)
class HostedCall:
    """Вызов тела инструмента хоста: имя инструмента, идентификатор вызова и
    аргументы тела.

    Создаёт его HostedTool из узла вызова после проверки аргументов по
    схеме; получают обвязки (CallHooks) и тело. Обвязки дополняют и снимают
    аргументы в словаре kwargs на месте: служебные значения подставляются до
    тела, подпись вызова снимается.
    """

    tool: str
    key: str
    kwargs: dict[str, Any]


AsyncCall: TypeAlias = Callable[[HostedCall], Awaitable[ToolResultBase]]
"""Тело инструмента хоста: вызов → результат семейства."""


class DirectCalls(NodeCalls):
    """Реализация NodeCalls прямым вызовом тела: у процесса нет наблюдателей
    вызова. Создаётся сборкой сервиса и стендами тестов."""

    async def conducted(
        self, card: ToolCard, node: DagNode, body: NodeBody
    ) -> NodeOutcome:
        return await body(node)


@dataclass
class HostedTool:
    """Исполняемый инструмент хоста: имя, описание, схема аргументов и тело.

    Создаёт его ToolHosting из PayloadTool фасада (тело исполняется в своём
    процессе) либо обёртка запуска ToolProcessWrap (тело — перенос вызова в
    отдельный процесс). Поля изменяемы: обвязки (ToolBody) оборачивают тело,
    источники значений и поля вызова пересобирают схему. views — вид
    аргументов вызова для ленты клиента по модели вызова автора: пересборка
    схемы его не меняет; None — модели вызова у инструмента нет. run() исполняет
    один вызов: проверяет аргументы по схеме и зовёт тело объектом
    HostedCall; call() — тот же вызов узлом DagNode с итогом NodeOutcome,
    его зовёт исполнитель узлов (DagRunner) через NodeCalls.
    """

    name: str
    description: str
    args_schema: type[BaseModel]
    views: Mapping[str, FieldView] | None
    body: AsyncCall
    _outcomes: NodeOutcomes = field(
        default_factory=NodeOutcomes, init=False, repr=False, compare=False
    )

    def copy(self) -> HostedTool:
        """Копия под обвязки: исходный инструмент остаётся нетронутым."""
        return replace(self)

    async def call(self, node: DagNode) -> NodeOutcome:
        """Исполнить вызов node и отдать его итог.

        Ошибки:
        pydantic.ValidationError — аргументы не проходят схему инструмента.
        TypeError — тело вернуло не результат семейства ToolResultBase.
        """
        result = await self.run(node.key, node.args)

        return self._outcomes.of(node, result, False)

    async def run(self, call_id: str, args: Mapping[str, Any]) -> ToolResultBase:
        """Исполнить вызов с идентификатором call_id и аргументами args и
        отдать результат тела. Аргументы — значения процесса хоста: кроме
        присланных моделью, ими бывают готовые объекты.

        Ошибки:
        pydantic.ValidationError — аргументы не проходят схему инструмента.
        TypeError — тело вернуло не результат семейства ToolResultBase.
        """
        call = HostedCall(tool=self.name, key=call_id, kwargs=self._validated(args))
        result = await self.body(call)
        if not isinstance(result, ToolResultBase):
            msg = (
                f"tool {self.name!r} must return a ToolResultBase model, "
                f"got {type(result).__name__}"
            )
            raise TypeError(msg)

        return result

    def _validated(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """Аргументы тела: каждое поле схемы значением проверенной модели —
        присланным либо дефолтом; чего в схеме нет, до тела не доходит."""
        call = self.args_schema.model_validate(dict(args))

        kwargs: dict[str, Any] = {}
        for name in self.args_schema.model_fields:
            kwargs[name] = getattr(call, name)

        return kwargs


class ToolSchema:
    """Схема аргументов инструмента: пересборка полей и JSON-схема вызова.

    Единственное место, где поля схемы добавляются и снимаются и где схема
    превращается в карточку для клиента. Создаётся каждым, кто правит схему
    (источники значений, поля вызова, поля каналов) либо отдаёт карточки
    (исполнитель, порт инструментов).
    """

    def rebuild(
        self,
        schema: type[BaseModel],
        add: Mapping[str, tuple[Any, Any]],
        drop: Iterable[str],
    ) -> type[BaseModel]:
        """Та же схема с добавленными полями (аннотация, дефолт) и без снятых.

        База модели сохраняется: показы вызова (ToolCallBase и класс вызова
        тула) живут на ней, а не на пересобранном наследнике.
        """
        dropped = frozenset(drop)

        fields: dict[str, Any] = {}
        for name, info in schema.model_fields.items():
            if name in dropped:
                continue

            fields[name] = (info.annotation, info)

        for name, declared in add.items():
            fields[name] = declared

        base = schema.__bases__[0]

        return create_model(schema.__name__, __base__=base, **fields)

    def card_of(self, tool: HostedTool) -> ToolCard:
        """Карточка инструмента хоста tool."""
        return self.card(tool.name, tool.description, tool.args_schema, tool.views)

    def card(
        self,
        name: str,
        description: str,
        schema: type[BaseModel],
        views: Mapping[str, FieldView] | None,
    ) -> ToolCard:
        """Карточка инструмента: JSON-схема вызова названа именем инструмента
        и описана его описанием; views — вид аргументов для ленты клиента."""
        return ToolCard(
            name=name,
            description=description,
            parameters=self._call_schema(name, description, schema),
            views=views,
        )

    def cards_of(self, tools: Iterable[HostedTool]) -> dict[str, ToolCard]:
        """Карточки инструментов tools по их именам, в порядке инструментов."""
        cards: dict[str, ToolCard] = {}
        for tool in tools:
            cards[tool.name] = self.card_of(tool)

        return cards

    def _call_schema(
        self, name: str, description: str, schema: type[BaseModel]
    ) -> dict[str, Any]:
        """JSON-схема вызова: поля схемы с их описанием, дефолтом и
        ограничениями; служебные атрибуты модели вызова в неё не идут."""
        fields: dict[str, Any] = {}
        for field_name, info in schema.model_fields.items():
            fields[field_name] = (info.annotation, self._shown(info))

        model = create_model(
            name, __config__=ConfigDict(arbitrary_types_allowed=True), **fields
        )
        model.__doc__ = textwrap.dedent(description)

        return model.model_json_schema()

    @staticmethod
    def _shown(info: FieldInfo) -> FieldInfo:
        """Поле для схемы клиента: описание, дефолт и метадата ограничений."""
        if info.default_factory is not None:
            shown = FieldInfo(
                description=info.description, default_factory=info.default_factory
            )
        else:
            shown = FieldInfo(description=info.description, default=info.default)

        if info.metadata:
            shown.metadata = info.metadata

        return shown


class ToolHosting:
    """PayloadTool фасада -> HostedTool с телом в своём процессе.

    Тело автора принимает аргументы своей подписью (либо вызов своим
    классом) и исполняется методом PayloadTool.result; тело хоста зовёт его
    аргументами вызова HostedCall. Создаётся сборками собственных
    инструментов процесса, встроенных узлов и стендами тестов; инструменты
    секций плагинов собирает обёртка запуска (ToolProcessWrap).
    """

    def __init__(self) -> None:
        self._views = CallViews()

    def hosted(self, tool: PayloadTool) -> HostedTool:
        return HostedTool(
            name=tool.name,
            description=tool.description,
            args_schema=tool.args_schema,
            views=self._views.of(tool.args_schema),
            body=partial(self._authored, tool),
        )

    def toolset(self, tools: Sequence[PayloadTool]) -> tuple[HostedTool, ...]:
        """TOOLS модуля инструментов -> инструменты хоста для реестра."""
        hosted: list[HostedTool] = []
        for tool in tools:
            hosted.append(self.hosted(tool))

        return tuple(hosted)

    @staticmethod
    def modules_of(tools: Sequence[PayloadTool]) -> tuple[str, ...]:
        """Уникальные модули тел, в порядке объявления."""
        modules: list[str] = []
        for tool in tools:
            module = ToolAddress.of(tool).module
            if module not in modules:
                modules.append(module)

        return tuple(modules)

    @staticmethod
    async def _authored(tool: PayloadTool, call: HostedCall) -> ToolResultBase:
        return await tool.result(call.kwargs, True)
