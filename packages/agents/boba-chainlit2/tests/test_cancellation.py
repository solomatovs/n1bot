"""Тесты механизма остановки хода: флаг, прерыватели, guard инструментов."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest
from langchain_core.tools import tool

from boba.chainlit2.agent.tools import build_bash_tool
from boba.tool.shell.config import BashSandboxConfig
from boba.toolkit.cancellation import (
    CancellableTools,
    ToolStopped,
    TurnCancellation,
    current_cancellation,
    turn_cancellation,
)
from boba.toolkit.http import CancellableHttpTransport
from boba.toolkit.result import ErrorResult
from boba.toolkit.sandbox import SandboxProfile, SandboxToolConfig
from boba.transport.http import HttpProfile, HttpRequest


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    "механизм остановки не зависит от сессии chainlit"


_HOST_RO_BINDS = ("/usr", "/bin", "/sbin", "/lib", "/lib64")


def _sandbox_config() -> BashSandboxConfig:
    """Минимальный профиль без образа: нужен лишь долгоживущий процесс."""
    profile = SandboxProfile.model_validate(
        {
            "rootfs": "",
            "ro_binds": _HOST_RO_BINDS,
            "rw_binds": (),
            "rw_images": (),
            "image_template": "",
            "launcher": {
                "mount_wait_sec": 10.0,
                "mount_poll_sec": 0.05,
                "shutdown_wait_sec": 5.0,
                "copy_chunk_bytes": 1 << 20,
            },
            "tmpfs": ("/tmp:16M",),  # noqa: S108
            "network": False,
            "env_set": {"PATH": "/usr/bin:/bin"},
            "timeout_sec": 300,
            "max_memory_bytes": 512 * 1024 * 1024,
            "max_cpu_sec": 300,
            "max_file_size_bytes": 64 * 1024 * 1024,
            "max_open_files": 256,
            "max_processes": 64,
            "max_output_bytes": 256 * 1024,
            "cgroup_base": "",
            "oom_score_adj": 0,
            "cwd": "/tmp",  # noqa: S108
        }
    )
    return BashSandboxConfig(
        sandbox=SandboxToolConfig(profile=profile, override={}),
    )



class TestTurnCancellation:
    def test_not_cancelled_initially(self) -> None:
        assert TurnCancellation().cancelled is False

    def test_cancel_sets_flag_and_raises(self) -> None:
        c = TurnCancellation()
        c.cancel()
        assert c.cancelled is True
        with pytest.raises(ToolStopped):
            c.raise_if_cancelled()

    def test_abort_is_called_on_cancel(self) -> None:
        c = TurnCancellation()
        called: list[str] = []
        with c.abort_with(lambda: called.append("aborted")):
            assert called == []
            c.cancel()
        assert called == ["aborted"]

    def test_abort_unregistered_after_block(self) -> None:
        c = TurnCancellation()
        called: list[str] = []
        with c.abort_with(lambda: called.append("aborted")):
            pass
        c.cancel()
        assert called == []

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
        assert called == ["second"]

    def test_cancel_is_idempotent(self) -> None:
        c = TurnCancellation()
        called: list[str] = []
        with c.abort_with(lambda: called.append("x")):
            c.cancel()
            c.cancel()
        assert called == ["x"]

    def test_visible_from_worker_thread(self) -> None:
        "инструменты исполняются в тред-пуле langchain — флаг обязан доезжать"
        with turn_cancellation() as c:
            ctx = copy_context()
            c.cancel()
            with ThreadPoolExecutor(1) as pool:
                seen = pool.submit(ctx.run, lambda: current_cancellation().cancelled)
                assert seen.result() is True

    def test_wait_returns_true_when_cancelled(self) -> None:
        c = TurnCancellation()
        threading.Timer(0.05, c.cancel).start()
        assert c.wait(5.0) is True


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
        assert echo.invoke({"text": "hi"}) == "hi"

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
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()

    def test_cancel_aborts_in_flight_request(self, drip_url: str) -> None:
        def read_all() -> int:
            profile = HttpProfile(base_url=drip_url)
            with (
                CancellableHttpTransport(profile) as transport,
                transport.fetch(HttpRequest(url=f"{drip_url}/slow")) as resp,
            ):
                return len(resp.stream.read(-1))

        with turn_cancellation() as c:
            ctx = copy_context()
            with ThreadPoolExecutor(1) as pool:
                future = pool.submit(ctx.run, read_all)
                threading.Event().wait(1.0)
                started = time.monotonic()
                c.cancel()
                with pytest.raises(httpx.HTTPError):
                    future.result(timeout=20)
                elapsed = time.monotonic() - started
        assert elapsed < self.ABORT_DEADLINE_SEC, (
            f"обрыв занял {elapsed:.1f}с — запрос дочитывался, а не прерывался"
        )


class TestSubprocessAbort:
    """Главный случай: отмена обязана убивать процесс, а не только await."""

    DURATION = "5931.17"
    """Уникальная длительность sleep: она видна в argv и после exec'а bash."""

    KILL_DEADLINE_SEC = 2.5
    """Порог отсекает запасной proc.wait(timeout=5) в _pump: без прерывателя
    процесс тоже умирает, но лишь через пять секунд после остановки."""

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
        tool_ = build_bash_tool(_sandbox_config(), dict)
        with turn_cancellation() as c:
            ctx = copy_context()
            with ThreadPoolExecutor(1) as pool:
                future = pool.submit(
                    ctx.run,
                    lambda: tool_.invoke({"command": f"sleep {self.DURATION}",
                                          "stdin": ""}),
                )
                assert c.wait(0.0) is False
                threading.Event().wait(1.5)
                assert self._running() >= 1, "процесс не стартовал — замер невалиден"
                started = time.monotonic()
                c.cancel()
                with pytest.raises(ToolStopped):
                    future.result(timeout=15)
                elapsed = time.monotonic() - started
        assert self._running() == 0, "процесс пережил остановку хода"
        assert elapsed < self.KILL_DEADLINE_SEC, (
            f"остановка заняла {elapsed:.1f}с — процесс убит не прерывателем, "
            "а запасным таймаутом"
        )
