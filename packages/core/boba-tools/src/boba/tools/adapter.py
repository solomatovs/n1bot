"""
`DishkaTool` — bridge между декларативным callable-стилем (`@tool`) и
streaming-only `Tool[Args, Cfg]` ABC из `boba.tools.domain`.

Снаружи неотличим от обычных tools — `ToolRegistry`/`ToolCatalog`/
`ToolExecutor` не знают, что внутри Dishka и Annotated-маркеры.

Контракт публичного API — только `Tool.stream(ctx, args) -> Iterator[ToolEvent]`.
Внутри `DishkaTool` диспетчеризует на два внутренних режима в зависимости от
**один-раз** определённого на этапе introspect признака `plan.is_generator`:

- **plain function** (`def f(...) -> T: return ...`) —
  `stream` зовёт target один раз, coerce-ит результат в `ToolResult` и
  yield-ит ровно одно `ToolStreamCompleted`.

- **generator function** (`def f(...) -> Generator[ToolEvent, None, T]: ...`) —
  `stream` пробрасывает все yield'ы напрямую (это `ToolProgressReported`-
  индикаторы), а финальный `return`-value (StopIteration.value) coerce-ит в
  `ToolResult` и yield-ит как терминальный `ToolStreamCompleted`.

В обоих случаях ровно одно `ToolStreamCompleted` гарантированно завершает
поток — это и есть «последнее событие = tool result».
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
            source_id, ToolName(plan.name),
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
        self, ctx: ToolContext, args: BaseModel,
    ) -> Iterator[ToolEvent]:
        """Единственный публичный entry-point.

        Резолвит DI-deps, зовёт target, и в зависимости от
        `plan.is_generator` либо пробрасывает yield-поток generator-tool'а,
        либо оборачивает результат plain-функции в один `ToolStreamCompleted`.
        """
        del ctx
        llm_kwargs = {
            name: getattr(args, name)
            for name in self._plan.args_model.model_fields
        }
        # Открываем request-scope: APP-scope deps идут через parent,
        # REQUEST-scope создаются здесь и закрываются на __exit__.
        # `component=` — параметр `get()`, не `container()`. Для каждого
        # резолва указываем component плагина — Dishka вернёт его версию,
        # либо (через alias() в Provider'е) свалится в default component.
        with self._container() as request_container:
            di_kwargs = {
                dep.param_name: request_container.get(
                    dep.target_type, component=self._component,
                )
                for dep in self._plan.di_deps
            }
            if self._plan.is_generator:
                yield from self._stream_from_generator(
                    self._target(**llm_kwargs, **di_kwargs),
                )
            else:
                raw_result = self._target(**llm_kwargs, **di_kwargs)
                yield ToolStreamCompleted(
                    result=_coerce_to_tool_result(
                        self._tool_id_value, raw_result,
                    ),
                )

    def _stream_from_generator(
        self, gen: Generator[Any, None, Any],
    ) -> Iterator[ToolEvent]:
        """Пробросить yield'ы generator-target'а + терминальный `ToolStreamCompleted`.

        Convention generator-tool'а:
        - `yield ToolProgressReported(...)` — индикативный прогресс.
        - `return final_result` — финальный результат (StopIteration.value),
          который framework coerce-ит в `ToolResult` и заворачивает в
          `ToolStreamCompleted`.

        Любой yield не-`ToolEvent`-типа — `ToolExecutionError`: контракт
        нарушен. Это даёт автору ясную ошибку, если он по ошибке yield-ит
        результат вместо return.
        """
        try:
            while True:
                try:
                    item = next(gen)
                except StopIteration as stop:
                    yield ToolStreamCompleted(
                        result=_coerce_to_tool_result(
                            self._tool_id_value, stop.value,
                        ),
                    )
                    return
                if isinstance(item, ToolProgressReported | ToolStreamCompleted):
                    yield item
                    continue
                msg = (
                    f"streaming tool {self._tool_id_value!r} yielded "
                    f"{type(item).__name__}; ожидается ToolEvent. "
                    f"Финальный результат возвращай через `return ...`."
                )
                raise ToolExecutionError(self._tool_id_value, msg)
        finally:
            gen.close()

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


def _coerce_to_tool_result(  # noqa: PLR0911
    tool_id: ToolId, value: Any,
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
