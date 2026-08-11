"""Обвязка bash внутри песочницы: команда из tool_args, её stdout — в tool_payload.

Команда запускается subprocess'ом: её stdin — канал tool_stdin узла, stderr
наследуется (после преамбулы это tool_stderr), stdout перекачивается в
tool_payload через PayloadChannels — bytes_out считает тот же код, что у
остальных инструментов. Квитанция пустая: процессные факты стадии собирает
раннер. Код возврата обвязки — код команды в шелльной форме.

Ошибки: PayloadError (command_not_started, command_output_unavailable) — уходит
конвертом в tool_result; ChannelError — контракт каналов нарушен.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from typing import IO, BinaryIO, ClassVar

from boba.tool.shell.protocol import BashFailure, BashRequest
from boba.toolkit.channels import Channel, ShellExit
from boba.toolkit.payload import (
    PayloadChannels,
    PayloadError,
    PayloadExit,
    PayloadLogging,
    PayloadOutputClosedError,
    PayloadStream,
)
from boba.toolkit.workflow import EmptyTrailer

logger = logging.getLogger(__name__)


class BashPayload:
    """Запуск команды и перекачка её stdout в канал данных стадии."""

    SHELL: ClassVar[tuple[str, ...]] = ("/bin/bash", "-c")
    CHUNK: ClassVar[int] = 65536

    @classmethod
    def main(cls) -> int:
        PayloadLogging.setup()

        channels = PayloadChannels.open()

        request = channels.args(BashRequest)

        try:
            rc = cls._run(request, channels)
        except PayloadError as exc:
            channels.write_error(exc.kind, exc.message)
            return int(PayloadExit.FAILURE)

        channels.write_result(EmptyTrailer())

        if rc != 0:
            return rc

        return int(channels.exit_code())

    @classmethod
    def _run(cls, request: BashRequest, channels: PayloadChannels) -> int:
        stream = channels.payload()

        proc = cls._spawn(request.command, cls._stdin_of(channels))

        output = proc.stdout
        if output is None:
            proc.kill()
            proc.wait()
            raise PayloadError(
                BashFailure.NO_OUTPUT_PIPE, "command stdout pipe is not available"
            )

        cls._copy(output, stream)

        # закрытие до wait: ушедший потребитель доставляет команде EPIPE
        output.close()

        return ShellExit.of(proc.wait())

    @staticmethod
    def _stdin_of(channels: PayloadChannels) -> BinaryIO | int:
        """Вход команды: канал стадии либо DEVNULL, когда входа у узла нет."""
        if channels.has(Channel.TOOL_STDIN):
            return channels.stdin()

        return subprocess.DEVNULL

    @classmethod
    def _spawn(
        cls,
        command: str,
        stdin: BinaryIO | int,
    ) -> subprocess.Popen[bytes]:
        argv = [*cls.SHELL, command]

        try:
            return subprocess.Popen(  # noqa: S603
                argv,
                stdin=stdin,
                stdout=subprocess.PIPE,
                bufsize=0,
                close_fds=True,
                env=cls._child_env(),
            )
        except OSError as exc:
            raise PayloadError(
                BashFailure.NOT_STARTED, f"command could not start: {exc}"
            ) from exc

    @classmethod
    def _copy(cls, source: IO[bytes], stream: PayloadStream) -> None:
        """Stdout команды в канал данных; уход потребителя прекращает продукцию."""
        while True:
            chunk = source.read(cls.CHUNK)

            if not chunk:
                return

            try:
                stream.write(chunk)
            except PayloadOutputClosedError:
                logger.info("payload consumer is gone, the command is cut off")
                return

    @staticmethod
    def _child_env() -> dict[str, str]:
        """Каналы стадии команде не принадлежат: их номера из окружения убираются."""
        env: dict[str, str] = {}

        for name, value in os.environ.items():
            if name.startswith(Channel.ENV_PREFIX):
                continue
            env[name] = value

        return env


if __name__ == "__main__":
    sys.exit(BashPayload.main())
