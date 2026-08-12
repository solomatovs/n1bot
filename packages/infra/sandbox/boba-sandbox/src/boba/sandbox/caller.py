"""Реализация порта запуска: любой вызов — граф стадий в WorkflowRunner.

Одиночный вызов фасада — вырожденный граф из одного узла; отдельного пути
исполнения нет.

Ошибки:
SandboxPayloadError — исполнение сорвалось либо контракт каналов нарушен.
PayloadFailureError — стадия сообщила об ожидаемом отказе конвертом; исключения
    sink'ов фасада (коллекторов) проходят наверх как есть — их контракт задаёт
    потребитель потока.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from boba.sandbox.runner import FanoutSink
from boba.sandbox.workflow import StageRegistry, WorkflowRunner
from boba.toolkit.channels import ChannelSink
from boba.toolkit.launcher import LauncherError, PayloadFailureError, ToolLauncher
from boba.toolkit.stream import StreamSink, ToolStreamTap
from boba.toolkit.workflow import (
    AccessPredicate,
    WorkflowError,
    WorkflowOutcome,
    WorkflowSpec,
)

__all__ = [
    "SandboxCaller",
    "SandboxPayloadError",
    "TapChannelSink",
]


class SandboxPayloadError(LauncherError):
    """Исполнение сорвалось или стадия нарушила контракт: итогу доверять нельзя."""


class TapChannelSink(ChannelSink):
    """ВРЕМЕННЫЙ адаптер: байты канала — в ToolStreamTap панели и журнала.

    Умирает на этапе 4 вместе с ToolStreamTap: рекордер журнала будет
    открывать сам исполнитель по контексту вызова.
    """

    def __init__(self, tap: StreamSink) -> None:
        self._tap = tap

    def feed(self, data: bytes) -> None:
        self._tap.feed(data)

    def close(self) -> None:
        """Тап живёт дольше вызова: его закрывает владелец рекордера."""


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
    ) -> None:
        self._runner = WorkflowRunner(registry, access, path_vars)

    def call(
        self,
        spec: WorkflowSpec,
        sinks: Mapping[str, ChannelSink] | None = None,
    ) -> WorkflowOutcome:
        """Исполнить граф; sinks — приёмники tool_payload по id узла.

        Коллекторы фасада читают продукт стадии по мере исполнения; трейлер
        фасад берёт из WorkflowOutcome по схеме реестра стадий.
        """
        taps = self._payload_taps(spec, sinks)
        stdout_taps = self._stdout_taps(spec)

        try:
            return self._runner.run(spec, taps, stdout_taps)
        except PayloadFailureError:
            raise
        except WorkflowError as exc:
            raise SandboxPayloadError(str(exc)) from exc

    def _payload_taps(
        self,
        spec: WorkflowSpec,
        sinks: Mapping[str, ChannelSink] | None,
    ) -> dict[str, ChannelSink]:
        """Sink'и фасада плюс временный tap живого потока (см. TapChannelSink)."""
        taps: dict[str, ChannelSink] = {}
        if sinks:
            taps.update(sinks)

        tap = ToolStreamTap.get()
        if tap is None:
            return taps

        for node in spec.nodes:
            adapter = TapChannelSink(tap)

            base = taps.get(node.id)
            if base is None:
                taps[node.id] = adapter
                continue

            # живой вывод первым: пользователь видит и байты, упёршиеся в лимит
            taps[node.id] = FanoutSink((adapter, base))

        return taps

    @staticmethod
    def _stdout_taps(spec: WorkflowSpec) -> dict[str, ChannelSink]:
        """Временный tap на tool_stdout каждого узла (этап 3, см. TapChannelSink)."""
        tap = ToolStreamTap.get()
        if tap is None:
            return {}

        taps: dict[str, ChannelSink] = {}
        for node in spec.nodes:
            taps[node.id] = TapChannelSink(tap)

        return taps
