"""Тесты механизма остановки хода: флаг, прерыватели, guard инструментов."""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from conftest import FakeUrl
from langchain_core.tools import tool

from boba.cancellation import (
    ToolStopped,
    TurnCancellation,
    current_cancellation,
    turn_cancellation,
)
from boba.chainlit.agent.toolrun.cancellation import CancellableTools
from boba.chainlit.agent.tools import BashToolConfig, build_bash_tool
from boba.chainlit.infra.plugins import as_structured_tool
from boba.sandbox import SandboxProfile, SandboxToolConfig
from boba.sandbox.zygote import ZygotePolicy, ZygoteRegistry, ZygoteToolCaller
from boba.toolkit.result import ErrorResult
from boba.transport.http import CancellableHttpTransport, HttpProfile, HttpRequest


def _bin_dirs() -> list[str]:
    """В тестах каталоги берутся из PATH; в проде их задаёт конфиг."""
    dirs: list[str] = []

    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry.startswith("/"):
            continue

        dirs.append(entry)

    return dirs


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    "механизм остановки не зависит от сессии chainlit"


_SANDBOX = Path(__file__).resolve().parents[4] / "build" / "src" / "sandbox"
_ROOTFS_IMAGE = _SANDBOX / "rootfs.ext4"
_SITE_PACKAGES = "/usr/local/lib/python3.11/site-packages"
_PACKAGES = Path(__file__).resolve().parents[3]

_SRC_PACKAGES = ("core/boba-cancellation", "core/boba-toolkit")
"""Пакеты, чей код нужен зиготе: их src приезжает биндом в /usr/src."""


def _python_path() -> str:
    parts: list[str] = []
    for name in _SRC_PACKAGES:
        parts.append(f"/usr/src/{name}/src")

    return os.pathsep.join(parts)


_ZYGOTE = ZygotePolicy(
    start_timeout_sec=60.0,
    max_start_attempts=1,
    restart_backoff_sec=0.05,
    healthy_after_sec=0.5,
    stop_wait_sec=5.0,
    call_poll_sec=0.05,
)


_PROFILE_RAW: dict[str, object] = {
    "host": {
        "mounting": {
            "mount_wait_sec": 10.0,
            "mount_poll_sec": 0.05,
            "shutdown_wait_sec": 5.0,
            "lock_wait_sec": 10.0,
            "copy_chunk_bytes": 1 << 20,
        },
        "binaries": {"dirs": _bin_dirs()},
        "stderr_tail_bytes": 4096,
        "channel_limit_bytes": 67108864,
        "fail_tail_chars": 2000,
        "kill_grace_sec": 5,
        "cgroup_base": "",
    },
    "rootfs": str(_ROOTFS_IMAGE),
    "mounts": {
        "ro": (
            f"{_SANDBOX / 'third' / 'python'}:/usr/local",
            f"{_SANDBOX / 'site'}:{_SITE_PACKAGES}",
            f"{_PACKAGES}:/usr/src",
        ),
        "rw": (),
        "tmp": "16M",  # noqa: S108
    },
    "isolation": {
        "network": False,
        "env": {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "PYTHONPATH": _python_path(),
            "HOME": "/tmp",  # noqa: S108
        },
        "reap_poll_sec": 0.05,
    },
    "limits": {
        "timeout_sec": 300,
        "process_memory_bytes": 512 * 1024 * 1024,
        "process_cpu_sec": 300,
        "process_file_bytes": 64 * 1024 * 1024,
        "process_open_files": 256,
        "process_oom_score_adj": 0,
    },
    "run": {
        "shell": "/bin/bash",
        "cwd": "/tmp",  # noqa: S108
    },
}


def _sandbox_config() -> SandboxToolConfig:
    """Минимальный профиль: нужен лишь долгоживущий процесс в песочнице."""
    profile = SandboxProfile.model_validate(_PROFILE_RAW)

    return SandboxToolConfig(profile=profile)


class TestTurnCancellation:
    def test_not_cancelled_initially(self) -> None:
        if TurnCancellation().cancelled is not False:
            raise AssertionError("TurnCancellation().cancelled is False")

    def test_cancel_sets_flag_and_raises(self) -> None:
        c = TurnCancellation()
        c.cancel()
        if c.cancelled is not True:
            raise AssertionError("c.cancelled is True")
        with pytest.raises(ToolStopped):
            c.raise_if_cancelled()

    def test_abort_is_called_on_cancel(self) -> None:
        c = TurnCancellation()
        called: list[str] = []
        with c.abort_with(lambda: called.append("aborted")):
            if called != []:
                raise AssertionError("called == []")
            c.cancel()
        if called != ["aborted"]:
            raise AssertionError('called == ["aborted"]')

    def test_abort_unregistered_after_block(self) -> None:
        c = TurnCancellation()
        called: list[str] = []
        with c.abort_with(lambda: called.append("aborted")):
            pass
        c.cancel()
        if called != []:
            raise AssertionError("called == []")

    def test_abort_with_refuses_to_start_when_cancelled(self) -> None:
        c = TurnCancellation()
        c.cancel()
        with pytest.raises(ToolStopped), c.abort_with(lambda: None):
            pass

    def test_failing_abort_does_not_block_others(self) -> None:
        c = TurnCancellation()
        called: list[str] = []

        def boom() -> None:
            raise RuntimeError("прерыватель сломан")

        with c.abort_with(boom), c.abort_with(lambda: called.append("second")):
            c.cancel()
        if called != ["second"]:
            raise AssertionError('called == ["second"]')

    def test_cancel_is_idempotent(self) -> None:
        c = TurnCancellation()
        called: list[str] = []
        with c.abort_with(lambda: called.append("x")):
            c.cancel()
            c.cancel()
        if called != ["x"]:
            raise AssertionError('called == ["x"]')

    def test_visible_from_worker_thread(self) -> None:
        "инструменты исполняются в тред-пуле langchain — флаг обязан доезжать"
        with turn_cancellation() as c:
            ctx = copy_context()
            c.cancel()
            with ThreadPoolExecutor(1) as pool:
                seen = pool.submit(ctx.run, lambda: current_cancellation().cancelled)
                if seen.result() is not True:
                    raise AssertionError("seen.result() is True")

    def test_wait_returns_true_when_cancelled(self) -> None:
        c = TurnCancellation()
        threading.Timer(0.05, c.cancel).start()
        if c.wait(5.0) is not True:
            raise AssertionError("c.wait(5.0) is True")


class TestToolGuard:
    @staticmethod
    def _tools() -> list:
        @tool
        def echo(text: str) -> str:
            """проба"""
            return text

        @tool
        def swallowing(text: str) -> ErrorResult:
            """инструмент, переводящий любую ошибку в ErrorResult"""
            return ErrorResult(message=text, error_kind="whatever")

        return CancellableTools.guard_all([echo, swallowing])

    def test_runs_normally_without_cancellation(self) -> None:
        echo, _ = self._tools()
        if echo.invoke({"text": "hi"}) != "hi":
            raise AssertionError('echo.invoke({"text": "hi"}) == "hi"')

    def test_refuses_to_start_after_cancel(self) -> None:
        echo, _ = self._tools()
        with turn_cancellation() as c:
            c.cancel()
            with pytest.raises(ToolStopped):
                echo.invoke({"text": "hi"})

    def test_result_after_cancel_is_stopped_not_error(self) -> None:
        "ErrorResult из-за оборванного транспорта не должен доехать до ленты"
        _, swallowing = self._tools()
        with turn_cancellation() as c:

            def _run() -> object:
                c.cancel()
                return swallowing.invoke({"text": "оборвано"})

            with pytest.raises(ToolStopped):
                _run()


class _DripHandler(BaseHTTPRequestHandler):
    """Отдаёт тело по капле: без обрыва чтение заняло бы десятки секунд."""

    CHUNKS = 100
    PAUSE_SEC = 0.5

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", str(self.CHUNKS * 1000))
        self.end_headers()
        for _ in range(self.CHUNKS):
            try:
                self.wfile.write(b"x" * 1000)
                self.wfile.flush()
            except OSError:
                return
            time.sleep(self.PAUSE_SEC)

    def log_message(self, *args: object) -> None:
        pass


class TestHttpAbort:
    """Скачивание страницы обязано обрываться остановкой, а не дочитываться."""

    ABORT_DEADLINE_SEC = 3.0

    @pytest.fixture
    def drip_url(self) -> Iterator[str]:
        server = HTTPServer(("127.0.0.1", 0), _DripHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            yield FakeUrl.loopback(server.server_port)
        finally:
            server.shutdown()

    def test_cancel_aborts_in_flight_request(self, drip_url: str) -> None:
        """Отмена приходит из чужого потока и обязана оборвать задачу запроса."""

        async def read_all() -> int:
            profile = HttpProfile(base_url=drip_url)
            async with (
                CancellableHttpTransport(profile) as transport,
                transport.fetch(HttpRequest(url=f"{drip_url}/slow")) as resp,
            ):
                return len(await resp.stream.read())

        async def stop_after(c: TurnCancellation, delay: float) -> None:
            await asyncio.to_thread(threading.Event().wait, delay)
            c.cancel()

        async def scenario() -> float:
            with turn_cancellation() as c:
                task = asyncio.ensure_future(read_all())
                await stop_after(c, 1.0)
                started = time.monotonic()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=20)
                return time.monotonic() - started

        elapsed = asyncio.run(scenario())
        if elapsed >= self.ABORT_DEADLINE_SEC:
            raise AssertionError(
                f"обрыв занял {elapsed:.1f}с — запрос дочитывался, а не прерывался"
            )


class TestSubprocessAbort:
    """Главный случай: отмена обязана убивать процесс, а не только await."""

    DURATION = "5931.17"
    """Уникальная длительность sleep: она видна в argv и после exec'а bash."""

    LIMITS = BashToolConfig(max_output_bytes=64 * 1024)
    """Потолок вывода: команда ничего не печатает, значение роли не играет."""

    KILL_DEADLINE_SEC = 2.5
    """Порог отсекает запасной proc.wait(timeout=5) в _pump: без прерывателя
    процесс тоже умирает, но лишь через пять секунд после остановки."""

    def teardown_method(self) -> None:
        ZygoteRegistry.stop_all()

    @classmethod
    def _running(cls) -> int:
        alive = 0
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes()
            except OSError:
                continue
            if cls.DURATION.encode() in cmdline:
                alive += 1
        return alive

    def test_cancel_kills_running_process(self) -> None:
        profile = _sandbox_config().profile

        supervisor = ZygoteRegistry.obtain("cancel-bash", profile, (), _ZYGOTE)
        caller = ZygoteToolCaller("cancel-bash", supervisor, profile)

        def launcher(tool: str) -> ZygoteToolCaller:
            return caller

        tool_ = as_structured_tool(build_bash_tool(self.LIMITS, launcher))
        with turn_cancellation() as c:
            ctx = copy_context()
            with ThreadPoolExecutor(1) as pool:
                future = pool.submit(
                    ctx.run,
                    lambda: tool_.invoke(
                        {"command": f"sleep {self.DURATION}", "stdin": ""}
                    ),
                )
                if c.wait(0.0) is not False:
                    raise AssertionError("c.wait(0.0) is False")
                threading.Event().wait(1.5)
                if self._running() < 1:
                    raise AssertionError("процесс не стартовал — замер невалиден")
                started = time.monotonic()
                c.cancel()
                with pytest.raises(ToolStopped):
                    future.result(timeout=15)
                elapsed = time.monotonic() - started
        if self._running() != 0:
            raise AssertionError("процесс пережил остановку хода")
        if elapsed >= self.KILL_DEADLINE_SEC:
            raise AssertionError(
                f"остановка заняла {elapsed:.1f}с — процесс убит не прерывателем, "
                "а запасным таймаутом"
            )
