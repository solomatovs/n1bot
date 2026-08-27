"""Процесс api целиком: python -m boba.api поднимается на конфиге стенда и отвечает."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from typing import ClassVar

import httpx
import pytest

pytestmark = pytest.mark.integration


class ApiProcess:
    """Дочерний api-процесс на порту стенда; ждёт openapi и гасит по SIGINT."""

    PORT: ClassVar[int] = 8613
    STARTUP_SEC: ClassVar[float] = 120.0
    STOP_SEC: ClassVar[float] = 30.0

    def __init__(self) -> None:
        env = dict(os.environ)
        env["BOBA_API_PORT"] = str(self.PORT)
        self.prefix = env["BOBA_URL_PREFIX"]
        self.base = f"http://127.0.0.1:{self.PORT}{self.prefix}/api"
        self.process = subprocess.Popen(
            [sys.executable, "-m", "boba.api"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def wait_ready(self) -> None:
        deadline = time.monotonic() + self.STARTUP_SEC
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                output = self._output()
                msg = f"api process exited early ({self.process.returncode}):\n{output}"
                raise RuntimeError(msg)

            try:
                reply = httpx.get(f"{self.base}/openapi.json", timeout=2.0)
            except httpx.HTTPError:
                time.sleep(1.0)
                continue

            if reply.status_code == httpx.codes.OK:
                return

            time.sleep(1.0)

        self.stop()
        msg = f"api process did not become ready in {self.STARTUP_SEC}s"
        raise RuntimeError(msg)

    def stop(self) -> str:
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)

        try:
            self.process.wait(self.STOP_SEC)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(self.STOP_SEC)

        return self._output()

    def _output(self) -> str:
        if self.process.stdout is None:
            return ""

        return self.process.stdout.read()


@pytest.fixture(scope="module")
def api() -> Iterator[ApiProcess]:
    process = ApiProcess()
    process.wait_ready()

    yield process

    output = process.stop()
    assert "Application shutdown complete" in output, output


def test_openapi_is_served_under_the_api_prefix(api: ApiProcess) -> None:
    reply = httpx.get(f"{api.base}/openapi.json")

    assert reply.status_code == 200
    paths = reply.json()["paths"]
    assert "/v1/workflows" in paths


def test_anonymous_request_is_unauthorized(api: ApiProcess) -> None:
    reply = httpx.get(f"{api.base}/v1/workflows")

    assert reply.status_code == 401


def test_workflow_socket_is_mounted(api: ApiProcess) -> None:
    reply = httpx.get(
        f"{api.base}/socket.io/", params={"EIO": "4", "transport": "polling"}
    )

    assert reply.status_code == 200
    assert "sid" in reply.text
