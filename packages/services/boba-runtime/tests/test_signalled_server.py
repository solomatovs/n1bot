"""Остановка сервера SignalledServer сигналом: настоящий процесс python с
uvicorn и приложением starlette, у которого есть lifespan.

Повторный сигнал посреди остановки uvicorn понимает как «не ждать
соединения»; приложение всё равно обязано закрыться своим lifespan shutdown
до кода после сервера, без отменённой задачи lifespan и трассировки в журнале.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

SERVER = """
import asyncio, logging, signal, socket, sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from boba.cancellation import ProcessStop
from boba.runtime.http import SignalledServer

trace = Path(sys.argv[1])
port_file = Path(sys.argv[2])
repeated = sys.argv[3] == "repeated"


def note(line):
    with trace.open("a") as out:
        out.write(f"{line}\\n")


class Noted:
    def __call__(self, received):
        note(f"action {received.name}")
        if repeated:
            signal.raise_signal(received)


@asynccontextmanager
async def lifespan(app):
    note("lifespan started")
    try:
        yield
    finally:
        note("lifespan closed")


async def ok(request):
    return PlainTextResponse("ok")


async def main():
    logging.basicConfig(level=logging.INFO)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port_file.write_text(str(listener.getsockname()[1]))
    stop = ProcessStop()
    stop.on_stop(Noted())
    app = Starlette(routes=[Route("/", ok)], lifespan=lifespan)
    server = SignalledServer(
        uvicorn.Config(app, log_config=None, timeout_graceful_shutdown=5), stop
    )
    await server.serve_until_stopped([listener])
    note("served")


asyncio.run(main())
"""

EXIT_SEC = 20.0


class ServerProcess:
    """Процесс сервера: запуск, ожидание ответа на порту, итог остановки."""

    def __init__(self, workdir: Path, mode: str) -> None:
        self._trace = workdir / "trace"
        self._port = workdir / "port"
        self._process = subprocess.Popen(
            [sys.executable, "-c", SERVER, str(self._trace), str(self._port), mode],
            stderr=subprocess.PIPE,
        )

    def wait_ready(self) -> None:
        deadline = time.monotonic() + EXIT_SEC
        while not self._answers():
            if time.monotonic() > deadline:
                raise AssertionError("the server never answered on its port")

            time.sleep(0.05)

    def stop(self, sent: signal.Signals) -> tuple[int, list[str], str]:
        self._process.send_signal(sent)
        _, stderr = self._process.communicate(timeout=EXIT_SEC)

        return (
            self._process.returncode,
            self._trace.read_text().splitlines(),
            stderr.decode(),
        )

    def _answers(self) -> bool:
        if not self._port.exists():
            return False

        port = self._port.read_text()
        if not port:
            return False

        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1.0):
                return True
        except (urllib.error.URLError, OSError):
            return False


@pytest.mark.parametrize("mode", ["single", "repeated"])
@pytest.mark.parametrize("sent", [signal.SIGINT, signal.SIGTERM])
def test_stop_closes_the_lifespan_before_the_code_after_the_server(
    tmp_path: Path, sent: signal.Signals, mode: str
) -> None:
    server = ServerProcess(tmp_path, mode)
    server.wait_ready()

    code, lines, stderr = server.stop(sent)

    if code != 0:
        raise AssertionError(f"the stopped server exits with 0, got {code}:\n{stderr}")

    expected = ["lifespan started", f"action {sent.name}", "lifespan closed", "served"]
    if lines != expected:
        raise AssertionError(
            f"the stop action runs, the lifespan closes, then the code after the "
            f"server: expected {expected}, got {lines}:\n{stderr}"
        )

    if "Traceback" in stderr:
        raise AssertionError(f"a stop leaves no traceback in the log:\n{stderr}")
