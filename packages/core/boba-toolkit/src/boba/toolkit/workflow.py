"""Доменные модели графа стадий: спецификация, контракты узлов и итог исполнения.

Структурные правила графа проверяются в самой модели при конструировании;
проверки, требующие реестра стадий (контракты, права), выполняет ContractCheck
с разрешёнными контрактами и предикатом прав, которые даёт sandbox-слой.

Ошибки: WorkflowError — спецификация невалидна, контрактное правило нарушено
или итог не содержит запрошенного; pydantic.ValidationError упаковывается на
границе разбора (WorkflowSpec.parse, WorkflowOutcome.trailer).
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
from typing import Any, Protocol, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    field_validator,
    model_validator,
)

from boba.toolkit.channels import (
    Channel,
    SafeSegment,
    StreamFormat,
    StreamKey,
    ValidationSummary,
)

__all__ = [
    "AccessPredicate",
    "ArgsEnricher",
    "ContractCheck",
    "EdgeSpec",
    "EmptyTrailer",
    "OutResolver",
    "RequestArgs",
    "StageArgsEnricher",
    "StageContract",
    "StageNode",
    "StageOutcome",
    "StageSpec",
    "WorkflowError",
    "WorkflowOutcome",
    "WorkflowSpec",
]

M = TypeVar("M", bound=BaseModel)


class WorkflowError(RuntimeError):
    """Спецификация графа невалидна либо итог не содержит запрошенного."""


class EmptyTrailer(BaseModel):
    """Трейлер без полей: квитанция несёт только факт успеха."""

    model_config = ConfigDict(extra="forbid")


class StageSpec(BaseModel):
    """Узел графа: инструмент реестра стадий и его пользовательские аргументы.

    stdin — литеральный вход узла: приложение пишет строку в tool_stdin и
    закрывает канал; это канал, а не аргумент инструмента.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    tool: str = Field(min_length=1)
    args: Mapping[str, JsonValue]
    stdin: str | None = None

    @field_validator("id")
    @classmethod
    def _safe_id(cls, value: str) -> str:
        """id узла едет в имя файла журнала: алфавит сегмента имени."""
        return SafeSegment.name(value)


class EdgeSpec(BaseModel):
    """Ребро графа: tool_payload источника подаётся в tool_stdin приёмника."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    src: str = Field(min_length=1)
    dst: str = Field(min_length=1)


class WorkflowSpec(BaseModel):
    """Граф стадий; структурные правила проверяются при конструировании.

    Правила: id узлов уникальны, рёбра ссылаются на существующие узлы, граф
    ацикличен, у узла не более одного входящего ребра, литерал stdin
    несовместим с входящим ребром.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    nodes: Sequence[StageSpec] = Field(min_length=1)
    edges: Sequence[EdgeSpec] = ()

    @classmethod
    def parse(cls, raw: Mapping[str, object]) -> WorkflowSpec:
        """Разбор сырой спецификации; любое нарушение — WorkflowError."""
        try:
            return cls.model_validate(raw)
        except ValidationError as exc:
            # сводка без значений полей: спека несёт args, написанные вызывающим
            summary = ValidationSummary.of(exc)
            raise WorkflowError(f"invalid workflow spec: {summary}") from exc

    def stage(self, stage_id: str) -> StageSpec:
        """Узел по id; неизвестный id — WorkflowError."""
        for node in self.nodes:
            if node.id == stage_id:
                return node

        raise WorkflowError(f"unknown stage: {stage_id}")

    def consumers_of(self, stage_id: str) -> tuple[str, ...]:
        """Приёмники продукта узла в порядке объявления рёбер."""
        consumers: list[str] = []
        for edge in self.edges:
            if edge.src == stage_id:
                consumers.append(edge.dst)

        return tuple(consumers)

    def order(self) -> tuple[str, ...]:
        """Топологический порядок id узлов: источники раньше приёмников."""
        return self._sorted_ids()

    @model_validator(mode="after")
    def _check_graph(self) -> WorkflowSpec:
        self._check_unique_ids()

        self._check_inputs()

        self._sorted_ids()

        return self

    def _check_unique_ids(self) -> None:
        seen: set[str] = set()
        for node in self.nodes:
            if node.id in seen:
                raise ValueError(f"duplicate stage id: {node.id}")
            seen.add(node.id)

    def _check_inputs(self) -> None:
        fed: set[str] = set()
        for edge in self.edges:
            if edge.dst in fed:
                raise ValueError(f"stage has more than one incoming edge: {edge.dst}")
            fed.add(edge.dst)

        for node in self.nodes:
            if node.stdin is None:
                continue
            if node.id in fed:
                raise ValueError(
                    f"stage has both a stdin literal and an incoming edge: {node.id}"
                )

    def _sorted_ids(self) -> tuple[str, ...]:
        graph: dict[str, set[str]] = {}
        for node in self.nodes:
            graph[node.id] = set()

        for edge in self.edges:
            if edge.src not in graph:
                raise ValueError(f"edge references unknown stage: {edge.src}")
            if edge.dst not in graph:
                raise ValueError(f"edge references unknown stage: {edge.dst}")
            graph[edge.dst].add(edge.src)

        sorter: TopologicalSorter[str] = TopologicalSorter(graph)

        try:
            return tuple(sorter.static_order())
        except CycleError as exc:
            raise ValueError(f"workflow graph has a cycle: {exc}") from exc


class StageContract(BaseModel):
    """Декларация продукта узла и схема его квитанции.

    out — объявленный формат канала данных для панели и человека; рёбра он не
    валидирует, по ним текут байты. None — узел канал данных не наполняет.
    result — схема трейлера квитанции.
    """

    model_config = ConfigDict(frozen=True)

    out: StreamFormat | None
    result: type[BaseModel]


class ArgsEnricher(Protocol):
    """args вызывающего -> полный запрос payload'а: op, соединения, лимиты.

    Обогатитель ничего не проверяет: обогащённый запрос валидируется моделью
    request узла, и её сводка ошибок точнее, чем тип исключения обогатителя.
    Значения не обязаны быть JSON-скалярами: профиль соединения остаётся
    моделью с SecretStr до сериализации запроса в tool_args, где раскрытие
    делает field_serializer модели запроса.
    """

    @abstractmethod
    def __call__(self, args: Mapping[str, JsonValue], /) -> Mapping[str, Any]:
        """Полный словарь запроса; валидируется моделью request узла."""
        ...


class RequestArgs:
    """Запрос-модель -> обогащённые args узла: поля остаются моделями.

    Секрет раскрывает только сериализация запроса в tool_args, поэтому
    обогатитель не имеет права на `model_dump(mode="json")`: он выполнил бы
    field_serializer профиля и положил пароль строкой в обычный словарь.
    """

    @classmethod
    def of(cls, request: BaseModel) -> Mapping[str, Any]:
        return dict(request)


class OutResolver(Protocol):
    """Разрешение формата продукта узла из валидированного запроса."""

    @abstractmethod
    def __call__(self, request: BaseModel, /) -> StreamFormat | None:
        """Формат tool_payload; None — потока данных наружу нет."""
        ...


class StageArgsEnricher:
    """args вызывающего плюс поля, которые задаёт узел: op, настройки, лимиты."""

    def __init__(self, settings: BaseModel) -> None:
        self._settings = settings.model_dump(mode="json")

    def __call__(self, args: Mapping[str, JsonValue], /) -> Mapping[str, Any]:
        request: dict[str, Any] = dict(args)
        request.update(self._settings)

        return request


@dataclass(frozen=True)
class StageNode:
    """Узел реестра стадий без профиля песочницы: его подставляет приложение.

    Профиль живёт в конфиге инструмента, поэтому реестр собирает приложение —
    пакет отдаёт контракт, entry-команду payload'а, модель запроса и
    обогатитель args. out_of — формат продукта как функция запроса; None —
    формат константен и объявлен контрактом.
    """

    contract: StageContract
    entry: tuple[str, ...]
    request: type[BaseModel]
    enrich: ArgsEnricher
    out_of: OutResolver | None = None


class AccessPredicate(Protocol):
    """Право вызывающего на инструмент; deny by default решает реализация."""

    @abstractmethod
    def __call__(self, tool: str, /) -> bool:
        """True — инструмент доступен вызывающему."""
        ...


class ContractCheck:
    """Проверки спеки, требующие реестра: разрешённый контракт узла и права.

    Контракты приходят разрешёнными, ключ — id узла; права проверяются
    предикатом по имени инструмента. Форматной проверки рёбер нет: по ребру
    текут байты, и годность потока выясняется исполнением стадии.
    """

    def __init__(
        self,
        contracts: Mapping[str, StageContract],
        access: AccessPredicate,
    ) -> None:
        self._contracts = contracts
        self._access = access

    def contract_of(self, stage_id: str) -> StageContract:
        """Разрешённый контракт узла; отсутствие — WorkflowError."""
        contract = self._contracts.get(stage_id)
        if contract is None:
            raise WorkflowError(f"no resolved contract for stage: {stage_id}")

        return contract

    def check(self, spec: WorkflowSpec) -> None:
        """Право на инструмент и контракт узла; нарушение — WorkflowError."""
        for node in spec.nodes:
            self._check_node(node)

    def _check_node(self, node: StageSpec) -> None:
        allowed = self._access(node.tool)
        if not allowed:
            raise WorkflowError(f"tool is not allowed: {node.tool}")

        self.contract_of(node.id)


class StageOutcome(BaseModel):
    """Процессный итог одной стадии.

    killed_by_runner — стадия убита каскадом или отменой, не своим сбоем;
    диагностика лимитов к такой стадии не применяется.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    stage: str = Field(min_length=1)
    exit_code: int
    duration_ms: int = Field(ge=0)
    timed_out: bool
    killed_by_runner: bool
    diagnostic: str


class WorkflowOutcome(BaseModel):
    """Итог исполнения графа: итоги стадий, сырые трейлеры и адреса журналов.

    journals — адреса журналов стадий; заполняет исполнитель журнала.
    """

    model_config = ConfigDict(frozen=True)

    stages: Sequence[StageOutcome]
    trailers: Mapping[str, JsonValue]
    journals: Sequence[StreamKey] = ()

    def outcome_of(self, stage: str) -> StageOutcome:
        """Итог стадии по id; отсутствие — WorkflowError."""
        for item in self.stages:
            if item.stage == stage:
                return item

        raise WorkflowError(f"no outcome for stage: {stage}")

    def journal_of(self, stage: str, channel: Channel) -> StreamKey | None:
        """Адрес журнала канала стадии; None — журнал этого канала не открылся."""
        for key in self.journals:
            if key.stage != stage:
                continue

            if key.channel is not channel:
                continue

            return key

        return None

    def trailer(self, stage: str, schema: type[M]) -> M:
        """Типизированный трейлер стадии; отсутствие или чужая схема — WorkflowError."""
        if stage not in self.trailers:
            raise WorkflowError(f"no trailer for stage: {stage}")

        raw = self.trailers[stage]

        try:
            return schema.model_validate(raw)
        except ValidationError as exc:
            raise WorkflowError(
                f"trailer of stage {stage} does not match {schema.__name__}"
            ) from exc
