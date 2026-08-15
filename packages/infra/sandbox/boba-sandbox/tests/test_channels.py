"""Каналы одного запуска: пайпы, насос, EOF, stdin больше буфера.

Проверяется на /bin/bash без bwrap: контракт насоса и раздачи дескрипторов
не зависит от песочницы, проброс через bwrap закреплён экспериментально.
"""

from __future__ import annotations

import time

from boba.sandbox.channels import ChannelSet, TailSink
from boba.sandbox.process_runner import run_subprocess
from boba.toolkit.channels import ToolChannel


class Collector:
    """Приёмник канала: копит байты в память."""

    def __init__(self) -> None:
        self.data = bytearray()

    def feed(self, chunk: bytes) -> None:
        self.data.extend(chunk)

    def text(self) -> str:
        return bytes(self.data).decode("utf-8")


def run_script(
    script: str,
    channels: ChannelSet,
    stdin_data: bytes = b"",
    timeout_sec: int = 30,
):
    """Скрипт под преамбулой редиректа — как его запустит SandboxCaller."""
    inner = f"{channels.redirect()}; {script}"

    env = {"PATH": "/usr/bin:/bin"}
    env.update(channels.env())

    try:
        return run_subprocess(
            ["/bin/bash", "-c", inner],
            stdin_data=stdin_data,
            timeout_sec=timeout_sec,
            cwd="/",
            env=env,
            stdout_sink=None,
            keep_stdout=True,
            pass_fds=tuple(channels.child_fds.values()),
            channel_sinks=channels.sinks(),
            on_spawn=channels.close_child_ends,
        )
    finally:
        channels.close()


def test_channels_are_separated() -> None:
    """stdout, stderr и result тела едут каждый своим каналом."""
    out = Collector()
    err = Collector()
    result = Collector()

    channels = ChannelSet.open(ToolChannel)
    channels.add_sink(ToolChannel.STDOUT, out.feed)
    channels.add_sink(ToolChannel.STDERR, err.feed)
    channels.add_sink(ToolChannel.RESULT, result.feed)

    result_env = ToolChannel.RESULT.env_name
    script = (
        'echo "body stdout"; '
        'echo "body stderr" >&2; '
        f'echo "envelope" >&${{{result_env}}}'
    )
    run = run_script(script, channels)

    assert run.exit_code == 0
    assert out.text() == "body stdout\n"
    assert err.text() == "body stderr\n"
    assert result.text() == "envelope\n"
    # stdout/stderr процесса bash (wrap-каналы) остались пустыми
    assert run.stdout == ""
    assert run.stderr == ""


def test_wrap_output_stays_on_process_streams() -> None:
    """Вывод до преамбулы — это wrap-каналы, а не каналы тела."""
    out = Collector()

    channels = ChannelSet.open(ToolChannel)
    channels.add_sink(ToolChannel.STDOUT, out.feed)

    inner = f'echo "wrap line"; {channels.redirect()}; echo "body line"'
    try:
        run = run_subprocess(
            ["/bin/bash", "-c", inner],
            stdin_data=b"",
            timeout_sec=30,
            cwd="/",
            env={"PATH": "/usr/bin:/bin"},
            stdout_sink=None,
            keep_stdout=True,
            pass_fds=tuple(channels.child_fds.values()),
            channel_sinks=channels.sinks(),
            on_spawn=channels.close_child_ends,
        )
    finally:
        channels.close()

    assert run.stdout == "wrap line\n"
    assert out.text() == "body line\n"


def test_pump_exits_on_eof_not_timeout() -> None:
    """Процесс завершился — насос вышел сразу, не досиживая таймаут."""
    channels = ChannelSet.open(ToolChannel)

    started = time.monotonic()
    run = run_script("true", channels, timeout_sec=600)
    elapsed = time.monotonic() - started

    assert run.exit_code == 0
    assert not run.timed_out
    assert elapsed < 5


def test_output_larger_than_pipe_buffer() -> None:
    """Мегабайт в канал не блокирует тело: насос вычитывает по мере записи."""
    out = Collector()

    channels = ChannelSet.open(ToolChannel)
    channels.add_sink(ToolChannel.STDOUT, out.feed)

    run = run_script("head -c 1048576 /dev/zero", channels)

    assert run.exit_code == 0
    assert len(out.data) == 1048576


def test_stdin_larger_than_pipe_buffer() -> None:
    """Вход больше буфера пайпа при одновременной записи тела: нет дедлока."""
    out = Collector()

    channels = ChannelSet.open(ToolChannel)
    channels.add_sink(ToolChannel.STDOUT, out.feed)

    payload = b"x" * 1048576
    run = run_script("cat", channels, stdin_data=payload)

    assert run.exit_code == 0
    assert len(out.data) == len(payload)


def test_stdin_unread_by_child_does_not_hang() -> None:
    """Тело не читает stdin и выходит — запись обрывается BrokenPipe, не висит."""
    channels = ChannelSet.open(ToolChannel)

    run = run_script("true", channels, stdin_data=b"y" * 1048576, timeout_sec=30)

    assert run.exit_code == 0
    assert not run.timed_out


def test_empty_channels_are_legal() -> None:
    """Тело ничего не написало — каналы пусты, это законный вид."""
    out = Collector()
    result = Collector()

    channels = ChannelSet.open(ToolChannel)
    channels.add_sink(ToolChannel.STDOUT, out.feed)
    channels.add_sink(ToolChannel.RESULT, result.feed)

    run = run_script("true", channels)

    assert run.exit_code == 0
    assert out.text() == ""
    assert result.text() == ""


def test_tail_sink_keeps_only_tail() -> None:
    tail = TailSink(max_bytes=8)

    tail.feed(b"0123456789")
    tail.feed(b"abcdef")

    assert tail.text() == "89abcdef"


def test_tail_sink_as_tee_on_channel() -> None:
    """TailSink ставится тройником к основному приёмнику того же канала."""
    err = Collector()
    tail = TailSink(max_bytes=16)

    channels = ChannelSet.open(ToolChannel)
    channels.add_sink(ToolChannel.STDERR, err.feed)
    channels.add_sink(ToolChannel.STDERR, tail.feed)

    run = run_script('echo "boom: traceback text" >&2', channels)

    assert run.exit_code == 0
    assert err.text() == "boom: traceback text\n"
    assert tail.text() == " traceback text\n"
