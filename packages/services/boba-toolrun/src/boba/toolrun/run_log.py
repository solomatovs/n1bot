"""Логи вызова инструмента и журнал его живого вывода.

Обвязка знает вызов целиком: имя и идентификатор вызова (HostedCall), исход
и длительность. Поэтому она же открывает журнал
живого вывода в CallJournals, ставит приёмники каналов в тап
исполнителя и закрывает журнал по исходу вызова. Здесь же снимается подпись
вызова (ToolIntent): её показывает лента, телу инструмента она не нужна.

Ошибки: своих не выпускает; исключение тела проходит наверх как есть.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from typing import ClassVar

from boba.identity.context import CallContexts
from boba.toolkit.calls import FieldMarks, ToolIntent
from boba.toolkit.chain import CallAmbient
from boba.toolkit.channels import CallOutcome
from boba.toolkit.failure import FailurePacker
from boba.toolkit.result import FailureResult, ToolResultBase
from boba.toolkit.timing import Elapsed
from boba.toolrun.hosted import HostedCall, HostedTool
from boba.toolrun.streams import CallJournals, ToolStream
from boba.toolrun.wrapping import CallHooks, ToolBody

__all__ = ["ToolRunLogger"]

logger = logging.getLogger(__name__)


@dataclass
class _CallScope:
    """Состояние одного вызова: журнал, таймер и исход для закрытия журнала."""

    name: str
    started: float
    entered: ExitStack
    """Контекст вызова инструмента, поставленный на время вызова."""
    stream: ToolStream | None
    note: str


class ToolRunLogger(CallHooks[_CallScope]):
    """Обвязка вызова инструмента: логи start/ok/failed, время, журнал вывода.

    Создаёт её загрузчик инструментов из журналов вызовов и держателя
    контекста процесса и ставит на все инструменты (guard_all). На время
    вызова она ставит контекст вызова моделью (инициатор llm с call_id),
    открывает журнал живого вывода в CallJournals и отдаёт его приёмники
    исполнителю через обстановку вызова (CallAmbient); по исходу вызова
    закрывает журнал.
    """

    ARGS_LIMIT: ClassVar[int] = 500

    def __init__(
        self, journals: CallJournals, contexts: CallContexts, ambient: CallAmbient
    ) -> None:
        self._journals = journals
        self._contexts = contexts
        self._ambient = ambient
        self._failures = FailurePacker()
        self._bodies = ToolBody()
        self._not_logged: dict[str, frozenset[str]] = {}
        """Аргументы, которые в лог не пишутся, по именам инструментов."""

    def guard_all(self, tools: Sequence[HostedTool]) -> list[HostedTool]:
        for tool in tools:
            self._not_logged[tool.name] = self._not_logged_of(tool)

        return self._bodies.hook_all(tools, self)

    async def before(self, call: HostedCall) -> _CallScope:
        name = call.tool
        call_id = call.key
        not_logged = self._not_logged.get(name, frozenset())
        self._log_start(name, call.kwargs, not_logged)
        ToolIntent.pop(call.kwargs)

        entered = ExitStack()
        journal_open = Elapsed()
        stream = self._open_stream(name, call_id, entered)
        if stream is not None:
            self._ambient.set_sinks(stream)
            logger.info(
                "tool[%s]: stream journal opened in %dms", name, journal_open.ms()
            )

        return _CallScope(
            name=name,
            started=time.monotonic(),
            entered=entered,
            stream=stream,
            note=str(CallOutcome.FAILED),
        )

    async def after(self, ctx: _CallScope, result: ToolResultBase) -> ToolResultBase:
        """Дописывает время вызова в результат."""
        ctx.note = str(CallOutcome.FINISHED)
        elapsed = self._elapsed_ms(ctx.started)
        self._log_outcome(ctx.name, ctx.started, result)

        return result.model_copy(update={"elapsed_ms": elapsed})

    async def on_error(self, ctx: _CallScope, error: Exception) -> ToolResultBase:
        self._log_failure(ctx.name, ctx.started, error)
        raise error

    async def cleanup(self, ctx: _CallScope) -> None:
        if ctx.stream is not None:
            self._ambient.set_sinks(None)
            ctx.stream.close(ctx.note)

        ctx.entered.close()

    def _open_stream(
        self, name: str, call_id: str, entered: ExitStack
    ) -> ToolStream | None:
        """Ставит контекст вызова инструмента и открывает его журнал.

        Вне контекста вызова ставить нечего и журнал привязать не к чему;
        без call_id журнала тоже нет.
        """
        context = self._contexts.peek()
        if context is None:
            return None

        entered.enter_context(self._contexts.applied(context.as_tool_call(call_id)))
        if not call_id:
            return None

        return self._journals.begin(
            context.subject.user_key, context.run_id, call_id, name
        )

    @staticmethod
    def _not_logged_of(tool: HostedTool) -> frozenset[str]:
        """Имена аргументов инструмента, помеченных в схеме NotLogged."""
        names: list[str] = []
        for name, field in tool.args_schema.model_fields.items():
            if FieldMarks.not_logged(field):
                names.append(name)

        return frozenset(names)

    @classmethod
    def _log_start(
        cls, name: str, kwargs: dict[str, object], not_logged: frozenset[str]
    ) -> None:
        logger.info(
            "tool[%s]: start args=%s", name, cls._render_args(kwargs, not_logged)
        )

    @staticmethod
    def _log_outcome(name: str, started: float, result: ToolResultBase) -> None:
        """Инструмент мог вернуть отказ вместо исключения — это не «ok»."""
        elapsed = ToolRunLogger._elapsed_ms(started)
        failure = ToolRunLogger._reported_failure(result)
        if failure is None:
            logger.info("tool[%s]: ok in %dms", name, elapsed)
            return
        logger.warning("tool[%s]: failed in %dms: %s", name, elapsed, failure)

    @staticmethod
    def _reported_failure(result: ToolResultBase) -> str | None:
        """Текст отказа из результата; None — инструмент отработал успешно."""
        if isinstance(result, FailureResult):
            return result.log_view()

        if result.ok:
            return None

        return result.llm_view()

    def _log_failure(self, name: str, started: float, error: BaseException) -> None:
        """Тот же результат-ошибка уходит LLM: лог и история совпадают."""
        logger.warning(
            "tool[%s]: failed in %dms: %s",
            name,
            ToolRunLogger._elapsed_ms(started),
            self._failures.pack(error).log_view(),
        )

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return int((time.monotonic() - started) * 1000)

    @classmethod
    def _render_args(cls, kwargs: dict[str, object], not_logged: frozenset[str]) -> str:
        """Аргументы вызова для лога; помеченные NotLogged не пишутся."""
        parts: list[str] = []
        for key, value in kwargs.items():
            if key in not_logged:
                continue

            parts.append(f"{key}={value!r}")
        text = ", ".join(parts)
        if len(text) > cls.ARGS_LIMIT:
            return text[: cls.ARGS_LIMIT] + "…"
        return text
