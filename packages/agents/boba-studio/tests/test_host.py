"""Процесс студии: python -m boba.studio отдаёт api под /api, страницу под /workflow."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

import httpx
import pytest

pytestmark = pytest.mark.integration

INDEX = """<!doctype html>
<html><head><!--BOBA_PAGE--><title>t</title></head>
<body><div id="root"></div></body></html>
"""


class StudioProcess:
    """Дочерний процесс студии на порту стенда с временным каталогом сборки."""

    PORT: ClassVar[int] = 8613
    STARTUP_SEC: ClassVar[float] = 120.0
    STOP_SEC: ClassVar[float] = 30.0

    def __init__(self, app_root: Path) -> None:
        env = dict(os.environ)
        env["BOBA_STUDIO_PORT"] = str(self.PORT)
        env["BOBA_APP_ROOT"] = str(app_root)
        env["BOBA_WORKFLOW_PAGE"] = "built"
        self.prefix = env["BOBA_URL_PREFIX"]
        self.root = f"http://127.0.0.1:{self.PORT}{self.prefix}"
        self.api = f"{self.root}/api"
        self.page = f"{self.root}/workflow"
        self.process = subprocess.Popen(
            [sys.executable, "-m", "boba.studio"],
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
                msg = f"studio exited early ({self.process.returncode}):\n{output}"
                raise RuntimeError(msg)

            try:
                reply = httpx.get(f"{self.api}/openapi.json", timeout=2.0)
            except httpx.HTTPError:
                time.sleep(1.0)
                continue

            if reply.status_code == httpx.codes.OK:
                return

            time.sleep(1.0)

        self.stop()
        msg = f"studio did not become ready in {self.STARTUP_SEC}s"
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
def studio(tmp_path_factory: pytest.TempPathFactory) -> Iterator[StudioProcess]:
    app_root = tmp_path_factory.mktemp("app_root")
    dist = app_root / "public" / "workflow"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(INDEX)
    (dist / "assets" / "app-1.js").write_text("export const built = true;")

    process = StudioProcess(app_root)
    process.wait_ready()

    yield process

    output = process.stop()
    assert "Application shutdown complete" in output, output


def test_openapi_is_served_under_the_api_prefix(studio: StudioProcess) -> None:
    reply = httpx.get(f"{studio.api}/openapi.json")

    assert reply.status_code == 200
    assert "/v1/workflows" in reply.json()["paths"]


def test_anonymous_request_is_unauthorized(studio: StudioProcess) -> None:
    reply = httpx.get(f"{studio.api}/v1/workflows")

    assert reply.status_code == 401


def test_workflow_socket_is_mounted(studio: StudioProcess) -> None:
    reply = httpx.get(
        f"{studio.api}/socket.io/", params={"EIO": "4", "transport": "polling"}
    )

    assert reply.status_code == 200
    assert "sid" in reply.text


def test_page_is_stamped_with_api_addresses(studio: StudioProcess) -> None:
    reply = httpx.get(f"{studio.page}/observe/abc")

    assert reply.status_code == 200
    assert f'<base href="{studio.prefix}/workflow/">' in reply.text
    assert f'"apiPrefix": "{studio.prefix}/api"' in reply.text
    assert f'"socketPath": "{studio.prefix}/api/socket.io"' in reply.text


def test_modules_are_served_from_dist(studio: StudioProcess) -> None:
    reply = httpx.get(f"{studio.page}/assets/app-1.js")

    assert reply.status_code == 200
    assert reply.text == "export const built = true;"
