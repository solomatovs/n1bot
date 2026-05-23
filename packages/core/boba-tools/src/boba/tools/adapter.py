"""
`DishkaTool` — bridge между декларативным callable-стилем (`@tool`) и
streaming-only `Tool[Args, Cfg]` ABC из `boba.tools.domain`.

Снаружи неотличим от обычных tools — `ToolRegistry`/`ToolCatalog`/
`ToolExecutor` не знают, что внутри Dishka и Annotated-маркеры.

Контракт публичного API — только `Tool.stream(ctx, args) -> Iterator[ToolEvent]`.
Tool-author **не оборачивает** ничего вручную; framework сам определяет
семантику каждого выхода tool'а:

- **`yield X`** → `ToolProgressReported` (индикативный прогресс).
- **`return X`** → `ToolStreamCompleted` (результат tool'а).

Никакого lookahead — события идут в UI без задержки. Plain function без
yield'ов (legacy-стиль `def f(...) -> X: return X`) работает так же:
её return становится результатом, прогрессов нет.

Поведение по `plan.is_generator`:

- **plain function** (`def f(...) -> T: return ...`) — DishkaTool зовёт
  target, coerce-ит результат в `ToolResult` и yield-ит ровно одно
  `ToolStreamCompleted`. Прогресса нет.

- **generator function** — DishkaTool iterate-ит yield'ы:
  - Каждый yield оборачивается в `ToolProgressReported`. Если tool сам
    yield-ил `ToolProgressReported` — passthrough; raw-значение → wrap
    в `ToolProgressReported(headline=str(value))`.
  - `return X` (StopIteration.value) оборачивается в `ToolStreamCompleted`.
  - **`yield ToolStreamCompleted(...)` — контракт нарушен**: TSC должен
    приходить через `return`, не yield. Framework бросит `ToolExecutionError`.

В любом случае ровно одно `ToolStreamCompleted` гарантированно завершает
поток.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Generator, Iterator
from typing import Any

from dishka import Container
from dishka.entities.component import Component
from pydantic import BaseModel, ValidationError

from boba.tools.domain.errors import (
    InvalidToolArgumentError,
    ToolExecutionError,
)
from boba.tools.domain.events import (
    ToolEvent,
    ToolProgressReported,
    ToolStreamCompleted,
)
from boba.tools.domain.ids import (
    ToolId,
    ToolName,
    ToolSourceId,
    compose_tool_id,
)
from boba.tools.domain.llm_schema import LLMSchemaGenerator
from boba.tools.domain.result import (
    ErrorResult,
    JsonResult,
    TextResult,
    ToolResult,
)
from boba.tools.domain.tool import Tool, ToolContext, ToolSchema
from boba.tools.introspect import CallPlan

__all__ = ["DishkaTool"]


class DishkaTool(Tool[BaseModel, None]):
    """Bridge: Annotated-callable + Dishka Container → streaming `Tool`.

    Состояние:
    - `_target` — собственно callable, который надо вызвать.
    - `_plan` — кеш pydantic args model + DI plan + `is_generator`-флаг,
      собранный один раз на этапе registration.
    - `_container` — root Dishka Container.
    - `_component` — Dishka component, в котором tool живёт (=plugin name).

    `_cfg` родителя у нас всегда None (cfg-параметры — через FromConfig DI).
    `_ctx` родителя у нас всегда None (контейнер — единственный resolver).
    """

    def __init__(
        self,
        target: Any,
        plan: CallPlan,
        container: Container,
        component: str,
        source_id: ToolSourceId,
    ) -> None:
        self._target = target
        self._plan = plan
        self._container = container
        self._component = Component(component)
        self._cfg = None  # type: ignore[assignment]
        self._ctx = None
        self._source_id = source_id
        self._tool_id_value: ToolId = compose_tool_id(
            source_id,
            ToolName(plan.name),
        )

    def tool_id(self) -> ToolId:
        return self._tool_id_value

    def name(self) -> ToolName:
        return ToolName(self._plan.name)

    def definition(self) -> ToolSchema:
        """JSON-schema для LLM. Описание — из docstring callable'а."""
        schema = self._plan.args_model.model_json_schema(
            schema_generator=LLMSchemaGenerator,
        )
        description = self._plan.description or schema.pop("description", "")
        schema.pop("description", None)
        return ToolSchema(
            name=self._tool_id_value,
            description=str(description),
            parameters_schema=schema,
        )

    def _args_model_class(self) -> type[BaseModel]:
        return self._plan.args_model

    def stream(
        self,
        ctx: ToolContext,
        args: BaseModel,
    ) -> Iterator[ToolEvent]:
        """
        Единственный публичный entry-point
        """
        del ctx
        # резолвим llm аргументы
        llm_kwargs = self._plan.get_llm_kwargs(args)

        with self._container() as request_container:
            # резолвим di аргументы
            di_kwargs = {
                dep.param_name: request_container.get(
                    dep.target_type, component=self._component
                )
                for dep in self._plan.di_deps
            }

            # если функция это генератор то запускаем ее как генератор событий
            if self._plan.is_generator:
                yield from self._stream_from_generator(
                    self._target(**llm_kwargs, **di_kwargs),
                )
            # если функция делает return то запускаем ее, ждем выполнения и отправляем
            # как одно единственное сгенерированное событие
            else:
                yield ToolStreamCompleted(
                    result=_coerce_to_tool_result(
                        self._tool_id_value,
                        self._target(**llm_kwargs, **di_kwargs),
                    ),
                )

    def _stream_from_generator(
        self,
        gen: Generator[Any, None, Any],
    ) -> Iterator[ToolEvent]:
        """yield → TPR, StopIteration.value → TSC. Без lookahead.

        Каждый yield немедленно проходит наружу обёрнутый в
        `ToolProgressReported` — UI получает события без задержки.
        Результат tool'а приходит **только** через `return X`, что
        framework ловит как `StopIteration.value` и заворачивает в
        `ToolStreamCompleted`.
        """
        return_value: Any = None
        try:
            while True:
                try:
                    item = next(gen)
                except StopIteration as stop:
                    return_value = stop.value
                    break
                yield self._wrap_progress(item)
        finally:
            gen.close()

        yield ToolStreamCompleted(
            result=_coerce_to_tool_result(
                self._tool_id_value,
                return_value,
            ),
        )

    def _wrap_progress(self, item: Any) -> ToolEvent:
        """Каждый yield → `ToolProgressReported`.

        - Tool yielded `ToolProgressReported` явно → passthrough (для richer
          details/severity).
        - Tool yielded `ToolStreamCompleted` → контракт нарушен: TSC должен
          приходить через `return`, не yield. `ToolExecutionError`.
        - raw-значение → `ToolProgressReported(headline=str(value))`.
        """
        if isinstance(item, ToolProgressReported):
            return item
        if isinstance(item, ToolStreamCompleted):
            msg = (
                f"streaming tool {self._tool_id_value!r} yielded "
                f"ToolStreamCompleted; результат tool'а должен приходить "
                f"через `return X`, а не `yield ToolStreamCompleted(X)`."
            )
            raise ToolExecutionError(self._tool_id_value, msg)
        return ToolProgressReported(headline=_format_progress_headline(item))

    def _parse_args(self, raw: dict[str, Any]) -> BaseModel:
        try:
            return self._plan.args_model.model_validate(raw)
        except ValidationError as e:
            errors = e.errors()
            first = errors[0] if errors else None
            loc = first.get("loc", ()) if first else ()
            field = str(loc[0]) if loc else ""
            msg = first.get("msg", str(e)) if first else str(e)
            raise InvalidToolArgumentError(
                self._tool_id_value,
                field or "<root>",
                msg,
            ) from e


def _format_progress_headline(item: Any) -> str:
    """Привести raw-значение к строке-headline для `ToolProgressReported`.

    Простое `str(item)`. Для dict'ов это `{'key': 'value'}` — читаемо;
    для primitives — само значение. Если нужен richer detail/severity,
    tool-author yield-ит `ToolProgressReported(...)` явно.
    """
    return str(item)


def _coerce_to_tool_result(  # noqa: PLR0911
    tool_id: ToolId,
    value: Any,
) -> ToolResult:
    """Привести возврат callable'а к `ToolResult` или бросить."""
    match value:
        case None:
            return TextResult(text="null")
        case TextResult() | JsonResult() | ErrorResult():
            return value
        case str():
            return TextResult(text=value)
        case bool() | int() | float():
            return TextResult(text=str(value))
        case BaseModel():
            return JsonResult(payload=value.model_dump())
        case _ if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return JsonResult(payload=dataclasses.asdict(value))
        case dict():
            return JsonResult(payload=value)
        case list() | tuple() | set() | frozenset():
            return JsonResult(payload=list(value))
        case _:
            msg = (
                f"tool {tool_id!r} вернул неподдерживаемый тип "
                f"{type(value).__name__} (ожидается ToolResult / str / int / "
                f"float / bool / list / tuple / set / dict / BaseModel / "
                f"dataclass / None)"
            )
            raise ToolExecutionError(tool_id, msg)
