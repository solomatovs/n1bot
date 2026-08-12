"""Реализация порта запуска: любой вызов — граф стадий в WorkflowRunner.

Одиночный вызов фасада — вырожденный граф из одного узла; отдельного пути
исполнения нет. Контекст вызова (адресат журнала) берётся здесь: ниже по
стеку он едет явным параметром. Исключения sink'ов фасада (коллекторов)
проходят наверх как есть — их контракт задаёт потребитель потока.

Ошибки:
SandboxPayloadError — исполнение сорвалось либо контракт каналов нарушен.
PayloadFailureError — стадия сообщила об ожидаемом отказе конвертом.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from boba.sandbox.journal import JournalError, StreamJournal
from boba.sandbox.runner import ToolCallContext
from boba.sandbox.workflow import StageRegistry, WorkflowRunner
from boba.toolkit.channels import ChannelSink, StreamKey
from boba.toolkit.launcher import (
    ChannelHead,
    PayloadFailureError,
    StageFailureError,
    ToolLauncher,
    WorkflowStageError,
)
from boba.toolkit.workflow import (
    AccessPredicate,
    WorkflowError,
    WorkflowOutcome,
    WorkflowSpec,
)

__all__ = [
    "SandboxCaller",
    "SandboxPayloadError",
]


class SandboxPayloadError(StageFailureError):
    """Исполнение сорвалось или стадия нарушила контракт: итогу доверять нельзя.

    Итоги стадий с адресами журналов приложены, когда стадии успели стартовать:
    по ним фасад читает голову канала сорвавшегося вызова.
    """


class SandboxCaller(ToolLauncher):
    """Реализация ToolLauncher: bwrap-песочница как окружение запуска.

    Делегирует WorkflowRunner'у: и вырожденный граф фасада, и граф
    инструмента workflow идут одним путём.
    """

    def __init__(
        self,
        registry: StageRegistry,
        access: AccessPredicate,
        path_vars: Callable[[], Mapping[str, str]],
        journal: StreamJournal,
    ) -> None:
        self._journal = journal
        self._runner = WorkflowRunner(registry, access, path_vars, journal)

    def call(
        self,
        spec: WorkflowSpec,
        sinks: Mapping[str, ChannelSink] | None = None,
    ) -> WorkflowOutcome:
        """Исполнить граф; sinks — приёмники tool_payload по id узла.

        Коллекторы фасада читают продукт стадии по мере исполнения; трейлер
        фасад берёт из WorkflowOutcome по схеме реестра стадий.
        """
        call = ToolCallContext.current()

        try:
            return self._runner.run(spec, call, sinks)
        except PayloadFailureError:
            raise
        except WorkflowStageError as exc:
            raise SandboxPayloadError(str(exc), exc.outcome) from exc
        except WorkflowError as exc:
            raise SandboxPayloadError(str(exc)) from exc

    def head(self, key: StreamKey, max_bytes: int) -> ChannelHead:
        """Голова журнала канала; журнал вызова к этому моменту дописан."""
        try:
            window = self._journal.head(key, max_bytes)
        except JournalError as exc:
            raise SandboxPayloadError(
                f"journal of {key.rel_log()} is not readable: {exc}"
            ) from exc

        if window is None:
            return ChannelHead.empty()

        truncated = window.end < window.size

        return ChannelHead(text=window.text, truncated=truncated)
