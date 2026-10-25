"""Инструмент хоста и его вызов: исполняемая сторона порта инструментов.

Инструмент хоста HostedTool — имя, описание, схема аргументов и одно
async-тело, на которое ложатся обвязки. Собственный инструмент процесса
строится из объявления фасада `@tool` (PayloadTool, ToolHosting);
инструмент плагина — из его объявления в манифесте, и аргументы модели
такого инструмента хост не проверяет, а передаёт телу как есть
(ToolInputs). Тело получает
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

from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import partial
from typing import Any, TypeAlias

from pydantic import BaseModel, create_model

from boba.toolkit.calls import CallSchema, CallViews, FieldView, SchemaMark
from boba.toolkit.dag import (
    DagNode,
    NodeBody,
    NodeCalls,
    NodeOutcome,
    NodeOutcomes,
    ToolCard,
    ToolHints,
)
from boba.toolkit.facade import PayloadTool
from boba.toolkit.manifest import ConfigConnection, InjectedRole
from boba.toolkit.result import ToolResultBase

__all__ = [
    "AsyncCall",
    "DirectCalls",
    "HostedCall",
    "HostedTool",
    "PluginSection",
    "ToolHosting",
    "ToolInputs",
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


@dataclass(frozen=True)
class PluginSection:
    """Секция [tool.<id>] плагина значениями JSON и профили соединений в ней
    по манифесту: её получают injected-параметры роли config. Собирает её
    загрузчик плагинов из конфига процесса."""

    values: Mapping[str, Any] = field(default_factory=dict)
    connections: tuple[ConfigConnection, ...] = ()


@dataclass(frozen=True)
class ToolInputs:
    """Входы инструмента плагина по его объявлению в манифесте.

    passed — JSON Schema аргументов модели: хост ставит их в карточку и
    отдаёт телу как есть, проверяет их тело. injected — роли служебных
    параметров, connections — параметры-соединения и их вид, section —
    секция плагина для роли config. Источники значений (ParamSource) ставят
    по ним свои обвязки. Пустой — собственный инструмент процесса: его
    аргументы описывает модель args_schema.
    """

    passed: Mapping[str, Any] = field(default_factory=dict)
    injected: Mapping[str, InjectedRole] = field(default_factory=dict)
    connections: Mapping[str, str] = field(default_factory=dict)
    section: PluginSection = field(default_factory=PluginSection)

    def properties(self) -> Mapping[str, Any]:
        """Аргументы модели: имя → схема."""
        declared = self.passed.get(SchemaMark.PROPERTIES)
        if not isinstance(declared, Mapping):
            return {}

        return declared

    def required(self) -> tuple[str, ...]:
        declared = self.passed.get("required")
        if not isinstance(declared, list):
            return ()

        names: list[str] = []
        for name in declared:
            names.append(str(name))

        return tuple(names)

    def defs(self) -> Mapping[str, Any]:
        declared = self.passed.get("$defs")
        if not isinstance(declared, Mapping):
            return {}

        return declared

    def not_logged(self) -> frozenset[str]:
        """Аргументы модели, значения которых в лог не идут."""
        return frozenset(SchemaMark.NOT_LOGGED.params(self.passed))

    def role_params(self, role: InjectedRole) -> tuple[str, ...]:
        """Служебные параметры роли role."""
        names: list[str] = []
        for name, declared in self.injected.items():
            if declared is role:
                names.append(name)

        return tuple(names)


@dataclass
class HostedTool:
    """Исполняемый инструмент хоста: имя, описание, схема аргументов и тело.

    Создаёт его ToolHosting из PayloadTool фасада (тело исполняется в своём
    процессе) либо обёртка запуска ToolProcessWrap по объявлению инструмента
    плагина (тело — перенос вызова в программу плагина, входы — inputs).
    Поля изменяемы: обвязки (ToolBody) оборачивают тело,
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
    hints: ToolHints = field(default_factory=ToolHints)
    inputs: ToolInputs = field(default_factory=ToolInputs)
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
        присланным либо дефолтом; аргументы модели плагина — как прислала
        модель; чего нет ни там, ни там, до тела не доходит."""
        call = self.args_schema.model_validate(dict(args))

        kwargs: dict[str, Any] = {}
        for name in self.args_schema.model_fields:
            kwargs[name] = getattr(call, name)

        for name in self.inputs.properties():
            if name in args:
                kwargs[name] = args[name]

        return kwargs


class ToolSchema:
    """Схема аргументов инструмента: пересборка полей и JSON-схема вызова.

    Единственное место, где поля схемы добавляются и снимаются и где схема
    превращается в карточку для клиента. Создаётся каждым, кто правит схему
    (источники значений, поля вызова, поля каналов) либо отдаёт карточки
    (исполнитель, порт инструментов). У инструмента плагина карточка
    сливает аргументы модели из манифеста с полями хоста.
    """

    def __init__(self) -> None:
        self._calls = CallSchema()

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
        return self.card(
            tool.name,
            tool.description,
            tool.args_schema,
            tool.views,
            tool.hints,
            tool.inputs,
        )

    def card(  # noqa: PLR0913 — карточка собирается из всех частей инструмента
        self,
        name: str,
        description: str,
        schema: type[BaseModel],
        views: Mapping[str, FieldView] | None,
        hints: ToolHints | None = None,
        inputs: ToolInputs | None = None,
    ) -> ToolCard:
        """Карточка инструмента: JSON-схема вызова названа именем инструмента
        и описана его описанием; views — вид аргументов для ленты клиента,
        hints — пометки для решения о подтверждении, None — без пометок;
        inputs — аргументы модели инструмента плагина, None — их нет."""
        if hints is None:
            hints = ToolHints()

        parameters = self._calls.of(name, description, schema.model_fields)
        if inputs is not None:
            parameters = self._merged(parameters, inputs)

        return ToolCard(
            name=name,
            description=description,
            parameters=parameters,
            views=views,
            output=None,
            hints=hints,
        )

    def cards_of(self, tools: Iterable[HostedTool]) -> dict[str, ToolCard]:
        """Карточки инструментов tools по их именам, в порядке инструментов."""
        cards: dict[str, ToolCard] = {}
        for tool in tools:
            cards[tool.name] = self.card_of(tool)

        return cards

    def clashes(self, tool: HostedTool) -> tuple[str, ...]:
        """Поля хоста, совпавшие с аргументами модели плагина или с её $defs."""
        clashing: list[str] = []
        for name in tool.args_schema.model_fields:
            if name in tool.inputs.properties():
                clashing.append(name)

        own = self._calls.of(tool.name, "", tool.args_schema.model_fields)
        for name in own.get("$defs", {}):
            if name in tool.inputs.defs():
                clashing.append(f"$defs/{name}")

        return tuple(clashing)

    @staticmethod
    def _merged(parameters: dict[str, Any], inputs: ToolInputs) -> dict[str, Any]:
        """Схема вызова: аргументы модели плагина, за ними поля хоста."""
        merged = dict(parameters)

        properties = dict(inputs.properties())
        properties.update(parameters.get(SchemaMark.PROPERTIES, {}))
        merged[SchemaMark.PROPERTIES] = properties

        required = list(inputs.required())
        required.extend(parameters.get("required", ()))
        if required:
            merged["required"] = required

        defs = dict(inputs.defs())
        defs.update(parameters.get("$defs", {}))
        if defs:
            merged["$defs"] = defs

        return merged


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
        """Инструмент хоста с телом автора в своём процессе."""
        return HostedTool(
            name=tool.name,
            description=tool.description,
            args_schema=tool.args_schema,
            views=self._views.of(tool.args_schema),
            body=partial(self._authored, tool),
            hints=tool.hints,
        )

    def toolset(self, tools: Sequence[PayloadTool]) -> tuple[HostedTool, ...]:
        """TOOLS модуля инструментов -> инструменты хоста для реестра."""
        hosted: list[HostedTool] = []
        for tool in tools:
            hosted.append(self.hosted(tool))

        return tuple(hosted)

    @staticmethod
    async def _authored(tool: PayloadTool, call: HostedCall) -> ToolResultBase:
        return await tool.result(call.kwargs, True)
