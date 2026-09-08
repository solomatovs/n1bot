"""Tool bash: shell-команда как модульный инструмент песочницы.

Тело исполняется в песочнице и запускает команду своим ребёнком. Команда,
завершившаяся кодом, отдаётся ShellResult с кодом, потоками и усечением;
команду, убитую сигналом, тело не объясняет — оно завершает вызов тем же
кодом (128 + сигнал), и смерть вызова разбирает обвязка запуска по нему.

Ошибки:
своих не выпускает: сбой команды — код возврата в ShellResult.
"""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from contextlib import suppress
from enum import IntEnum
from typing import Annotated, BinaryIO, ClassVar, Final

from pydantic import BaseModel, ConfigDict, Field

from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import Injected, tool
from boba.toolkit.launcher import ClippedText
from boba.toolkit.result import MarkdownResult, ShellResult

__all__ = ["TOOLS", "BashToolConfig", "bash"]


class BashInputLimit(IntEnum):
    """Потолок входа инструмента: длина команды."""

    COMMAND_CHARS = 16_384


class BashToolConfig(BaseModel):
    """Секция [tool.bash]: потолок вывода и таймаут команды."""

    model_config = ConfigDict(extra="ignore")

    SECTION: ClassVar[str] = "tool.bash"

    max_output_bytes: int = Field(
        gt=0,
        description="Аварийный потолок объёма каждого потока вывода (байт).",
    )
    timeout_sec: float = Field(
        gt=0,
        description="Сколько секунд команда может работать; дольше — timed_out.",
    )


class StreamCapture:
    """Поток команды: пересылается в свой поток тела как есть (живой вывод в
    ленте) и копится для результата в пределах бюджета усечения."""

    CHUNK: ClassVar[int] = 65_536

    def __init__(self, source: int, sink: BinaryIO, budget: int) -> None:
        self.source = source
        self._sink = sink
        self._budget = budget
        self._kept = bytearray()
        self._total = 0
        self.open = True

    def pump(self) -> None:
        """Прочитать порцию; пустое чтение — конец потока."""
        chunk = os.read(self.source, self.CHUNK)
        if not chunk:
            self.open = False
            return

        self._sink.write(chunk)
        self._sink.flush()

        self._total += len(chunk)
        room = self._budget - len(self._kept)
        if room > 0:
            self._kept.extend(chunk[:room])

    def text(self) -> ClippedText:
        """Собранный текст с пометкой об усечении по полному объёму."""
        decoded = bytes(self._kept).decode(BashOutput.ENCODING, errors="replace")
        truncated = self._total > len(self._kept)
        if not truncated:
            return ClippedText(text=decoded, total_bytes=self._total, truncated=False)

        notice = ClippedText.NOTICE.format(kept=len(self._kept), total=self._total)

        return ClippedText(
            text=f"{decoded}{notice}", total_bytes=self._total, truncated=True
        )


class BashOutput:
    """Запуск команды с потоковым выводом и сборка ShellResult."""

    ENCODING: ClassVar[str] = "utf-8"
    SHELL: ClassVar[tuple[str, ...]] = ("bash", "-c")

    SIGNAL_BASE: ClassVar[int] = 128
    """Код возврата убитого сигналом: 128 + номер сигнала, как считает shell."""

    TIMEOUT_EXIT: ClassVar[int] = 124
    """Код снятой по таймауту команды: как у coreutils timeout."""

    KILL_GRACE_SEC: ClassVar[float] = 1.0

    @classmethod
    def argv(cls, command: str) -> list[str]:
        return [*cls.SHELL, command]

    @classmethod
    def killed(cls, returncode: int) -> bool:
        """Команду убил сигнал: сам shell (код < 0) либо его последняя
        команда (shell отдаёт 128 + сигнал)."""
        if returncode < 0:
            return True

        return returncode > cls.SIGNAL_BASE

    @classmethod
    def exit_code(cls, returncode: int) -> int:
        """Код возврата вызова для убитой команды в соглашении shell."""
        if returncode < 0:
            return cls.SIGNAL_BASE - returncode

        return returncode

    @classmethod
    def run(cls, command: str, cfg: BashToolConfig) -> ShellResult:
        """Команда без stdin; потоки живут до выхода или таймаута."""
        started = time.monotonic()
        proc = subprocess.Popen(  # noqa: S603 — команда и есть назначение инструмента
            cls.argv(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        if proc.stdout is None or proc.stderr is None:
            msg = "bash: subprocess pipes were not created"
            raise RuntimeError(msg)

        out = StreamCapture(
            proc.stdout.fileno(), sys.stdout.buffer, cfg.max_output_bytes
        )
        err = StreamCapture(
            proc.stderr.fileno(), sys.stderr.buffer, cfg.max_output_bytes
        )
        timed_out = cls._pump_until_exit(proc, (out, err), started + cfg.timeout_sec)

        returncode = proc.wait()
        if timed_out:
            returncode = cls.TIMEOUT_EXIT

        duration_ms = int((time.monotonic() - started) * 1000)
        shown_out = out.text()
        shown_err = err.text()

        return ShellResult(
            ok=returncode == 0,
            exit_code=returncode,
            stdout=shown_out.text,
            stdout_bytes=shown_out.total_bytes,
            stdout_truncated=shown_out.truncated,
            stderr=shown_err.text,
            stderr_bytes=shown_err.total_bytes,
            stderr_truncated=shown_err.truncated,
            duration_ms=duration_ms,
            timed_out=timed_out,
        )

    @classmethod
    def _pump_until_exit(
        cls,
        proc: subprocess.Popen[bytes],
        streams: Sequence[StreamCapture],
        deadline: float,
    ) -> bool:
        """Качать потоки до их закрытия; за дедлайном — снять группу процессов."""
        selector = selectors.DefaultSelector()
        for stream in streams:
            selector.register(stream.source, selectors.EVENT_READ, stream)

        with selector:
            while any(stream.open for stream in streams):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    cls._kill_group(proc)
                    cls._drain(streams)
                    return True

                for key, _ in selector.select(timeout=remaining):
                    stream = key.data
                    stream.pump()
                    if not stream.open:
                        selector.unregister(stream.source)

        return False

    @classmethod
    def _kill_group(cls, proc: subprocess.Popen[bytes]) -> None:
        with suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)

        try:
            proc.wait(timeout=cls.KILL_GRACE_SEC)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)

    @staticmethod
    def _drain(streams: Sequence[StreamCapture]) -> None:
        """Дочитать, что осталось в пайпах убитой команды, без блокировки."""
        for stream in streams:
            os.set_blocking(stream.source, False)
            with suppress(BlockingIOError, OSError):
                while stream.open:
                    stream.pump()


@tool
def bash(
    command: Annotated[
        str,
        Field(
            min_length=1,
            max_length=BashInputLimit.COMMAND_CHARS,
            description="Shell-команда (передаётся в `bash -c`).",
        ),
        MarkdownResult(language="bash"),
    ],
    *,
    cfg: Annotated[BashToolConfig, Injected],
) -> ShellResult:
    """Выполнить shell-команду и вернуть вывод; доступ к ФС и сети ограничен.

    Stdin команды закрыт: входные данные передавайте самой командой
    (heredoc, printf | ..., файл). Объём вывода ограничивайте командой
    (head, tail, grep, wc): всё, что вышло за аварийный потолок, отброшено,
    а ответ помечен truncated.
    """
    result = BashOutput.run(command, cfg)

    if BashOutput.killed(result.exit_code):
        # команду убил сигнал ядра: вызов завершается тем же кодом, объяснение
        # по коду возврата даёт обвязка запуска, знающая лимиты профиля
        os._exit(BashOutput.exit_code(result.exit_code))

    return result


TOOLS: Final = ToolMain.toolset(bash)

if __name__ == "__main__":
    raise SystemExit(ToolMain.run(TOOLS))
