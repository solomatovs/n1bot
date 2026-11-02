"""Владение пайпами портов вызова для process и зиготы.

Ошибки:
LauncherError — порт уже передан читателю или ядро отказало в размере пайпа.
OSError — не удалось открыть или закрыть дескриптор.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack

from boba.tool_io.pump import PipePlumbing
from boba.toolkit.channels import ToolChannel
from boba.toolkit.launcher import LauncherError
from boba.toolkit.protocol import CallInputSpec, CallOutputSpec

__all__ = ["PipeSetup", "StreamPipes"]


class PipeSetup(ExitStack):
    """Собирает дескрипторы вызова с откатом через ExitStack при отказе.

    Лончер открывает пайпы через pipe(), регистрирует сокеты через enter_context()
    и передаёт владение готовому вызову методом pop_all().
    """

    def pipe(self) -> tuple[int, int]:
        read_fd, write_fd = os.pipe()
        self.callback(os.close, read_fd)
        self.callback(os.close, write_fd)

        return read_fd, write_fd


class StreamPipes:
    """Держит пайпы входных и выходных портов и передаёт их одному владельцу.

    Создаётся лончером вместе со служебными каналами через общий PipeSetup.
    CallInputs забирает входы, перекачка — выходы; остальные выходы читает
    насос. Способ закрытия дескриптора задаёт лончер.
    """

    def __init__(
        self,
        setup: PipeSetup,
        inputs: Sequence[CallInputSpec],
        outputs: Sequence[CallOutputSpec],
        close_fd: Callable[[int], None],
    ) -> None:
        self._close = close_fd
        self._out_specs = tuple(outputs)
        self._inputs_taken = False
        self._outputs_taken = False
        self._claimed: int | None = None
        self._ins = tuple(self._open(setup, inputs))
        self._outs = tuple(self._open(setup, outputs))

    def _open(
        self, setup: PipeSetup, specs: Sequence[CallInputSpec | CallOutputSpec]
    ) -> Iterator[tuple[int, int]]:
        for spec in specs:
            read_fd, write_fd = setup.pipe()
            PipePlumbing.require(write_fd, spec.pipe_bytes, spec.port)

            yield read_fd, write_fd

    def child_inputs(self) -> Iterator[int]:
        for read_fd, _ in self._ins:
            yield read_fd

    def child_outputs(self) -> Iterator[int]:
        for _, write_fd in self._outs:
            yield write_fd

    def take_inputs(self) -> tuple[int, ...]:
        if self._inputs_taken:
            raise LauncherError("call input pipes: already taken or closed")

        self._inputs_taken = True
        writers = (write_fd for _, write_fd in self._ins)

        return tuple(writers)

    def take_outputs(self) -> tuple[int, ...]:
        if self._outputs_taken:
            raise LauncherError("call output pipes: already taken or closed")

        if self._claimed is not None:
            raise LauncherError(
                "call output pipes: already claimed by the frame reader"
            )

        self._outputs_taken = True
        readers = (read_fd for read_fd, _ in self._outs)

        return tuple(readers)

    def claim_frames(self) -> int | None:
        if self._outputs_taken:
            raise LauncherError("call output pipes: already taken, cannot claim frames")

        if len(self._out_specs) != 1:
            return None

        if self._out_specs[0].raw:
            return None

        self._claimed = self._outs[0][0]

        return self._claimed

    def frame_reads(self) -> tuple[tuple[ToolChannel, int], ...]:
        if self._claimed is None:
            return ()

        return ((ToolChannel.FRAMES, self._claimed),)

    def drain_reads(self) -> Iterator[int]:
        """Выходы без читателя: насос дренирует их, чтобы тело не блокировалось."""
        if self._outputs_taken:
            return

        for read_fd, _ in self._outs:
            if read_fd != self._claimed:
                yield read_fd

    def close_child_ends(self) -> None:
        for fd in self.child_inputs():
            self._close(fd)

        for fd in self.child_outputs():
            self._close(fd)

    def close_host_ends(self) -> None:
        if not self._inputs_taken:
            self._inputs_taken = True
            for _, write_fd in self._ins:
                self._close(write_fd)

        if not self._outputs_taken:
            self._outputs_taken = True
            for read_fd, _ in self._outs:
                self._close(read_fd)
