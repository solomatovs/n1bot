"""Остановка сервиса сигналом: настоящий процесс boba-mcp с открытым
keep-alive соединением и идущим вызовом инструмента в песочнице.

Остановка обязана дойти до конца сама: соединения клиентов рвутся через
[mcp].shutdown_timeout_sec, идущий вызов гасится, зиготы останавливаются,
процесс выходит с кодом 0. Принудительное убийство (код 137 у контейнера)
означает, что до завершения запусков сервис не дошёл.

pytest -m integration.
"""

from __future__ import annotations

import asyncio
import signal
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from omegaconf import DictConfig, OmegaConf

from boba.auth.proxy import ProxyAssertions
from boba.stand.ui.stand import ServiceStand, StandProcess, free_port

pytestmark = pytest.mark.integration

BOOT_SEC = 300.0
ENDPOINT = "general"
SLEEP_SEC = 7391
"""Уникальная длительность sleep: по ней тест находит процесс тела вызова."""
EXIT_SEC = 25.0
"""Срок выхода после сигнала: ожидание соединений и остановка запусков."""


class RunningCall:
    """Долгий вызов инструмента клиентом MCP в своём потоке: держит запрос
    открытым, пока сервис его не оборвёт."""

    def __init__(self, url: str, token: str) -> None:
        self._url = url
        self._token = token
        self._ended: list[str] = []
        self._replies: list[str] = []
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def ended(self, timeout: float) -> bool:
        self._thread.join(timeout)

        return not self._thread.is_alive()

    def reply(self) -> str:
        """Текст ответа сервиса на вызов; вызов оборвался без ответа — отказ."""
        if not self._replies:
            raise AssertionError(f"the call got no reply, it ended as {self._ended}")

        return self._replies[0]

    def _run(self) -> None:
        try:
            asyncio.run(self._called())
        except Exception as exc:
            self._ended.append(type(exc).__name__)

    async def _called(self) -> None:
        headers = {"Authorization": f"Bearer {self._token}"}
        transport = StreamableHttpTransport(self._url, headers=headers)
        async with Client(transport) as client:
            result = await client.call_tool_mcp(
                "bash", {"command": f"sleep {SLEEP_SEC}"}
            )

        texts: list[str] = []
        for block in result.content:
            texts.append(str(getattr(block, "text", "")))

        self._replies.append(" ".join(texts))


@pytest.fixture
def service(
    tmp_path: Path, service_raw_config: DictConfig
) -> Iterator[StandProcess[ServiceStand]]:
    db_name = OmegaConf.select(service_raw_config, "postgres.dbname")
    if not isinstance(db_name, str):
        msg = (
            "shutdown stand: section [postgres] of the service config expects "
            f"dbname as a string, got {db_name!r}"
        )
        raise TypeError(msg)

    config = ServiceStand(
        workdir=tmp_path, app_port=free_port(), db_name=db_name, sandbox=True
    )
    process = StandProcess[ServiceStand](config=config, log_path=tmp_path / "mcp.log")
    process.start(BOOT_SEC)
    try:
        yield process
    finally:
        process.stop()


def _token(public: str) -> str:
    """Токен пользователя стенда обменом утверждения доверенного клиента."""
    resource = f"{public}/mcp/{ENDPOINT}"
    assertion = ProxyAssertions(ServiceStand.PROXY_SECRET, 0).issue(
        ServiceStand.CLIENT_ID, "ivanov", "ADM", public, resource
    )
    reply = httpx.post(
        f"{public}/token",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
            "client_id": ServiceStand.CLIENT_ID,
            "client_secret": ServiceStand.CLIENT_SECRET,
            "resource": resource,
        },
        timeout=30.0,
    )
    if reply.status_code != httpx.codes.OK:
        raise AssertionError(f"the stand user signs in: {reply.text}")

    return str(reply.json()["access_token"])


def _sleeping() -> bool:
    """Жив ли процесс тела вызова."""
    found = subprocess.run(
        ["/usr/bin/pgrep", "-f", f"sleep {SLEEP_SEC}"],
        capture_output=True,
        check=False,
    )

    return found.returncode == 0


def test_sigterm_stops_the_running_call_and_exits_cleanly(
    service: StandProcess[ServiceStand],
) -> None:
    public = f"http://127.0.0.1:{service.config.app_port}"
    process = service.process
    if process is None:
        raise AssertionError("the stand process is started")

    call = RunningCall(f"{public}/mcp/{ENDPOINT}", _token(public))
    call.start()
    deadline = time.monotonic() + 60.0
    while not _sleeping():
        if time.monotonic() > deadline:
            raise AssertionError(f"the tool body never started:\n{service.tail()}")

        time.sleep(0.2)

    with httpx.Client() as idle:
        if idle.get(f"{public}/health").status_code != httpx.codes.OK:
            raise AssertionError("the service answers before the stop")

        mark = service.log_lines()
        started = time.monotonic()
        process.send_signal(signal.SIGTERM)
        try:
            code = process.wait(timeout=EXIT_SEC)
        except subprocess.TimeoutExpired as exc:
            msg = (
                f"the service did not exit in {EXIT_SEC}s after SIGTERM with an "
                f"idle keep-alive connection and a running call:\n{service.tail()}"
            )
            raise AssertionError(msg) from exc

    elapsed = time.monotonic() - started
    if code != 0:
        raise AssertionError(
            f"the service exits with 0 after SIGTERM, got {code} in {elapsed:.1f}s:\n"
            f"{service.tail()}"
        )

    if not call.ended(10.0):
        raise AssertionError("the client of the running call is released")

    reply = call.reply()
    if "was stopped" not in reply or "shutdown" not in reply:
        raise AssertionError(f"the client is told the call was stopped: {reply}")

    complaints = service.complaints(mark)
    if complaints:
        raise AssertionError(f"a stop is not reported as a failure: {complaints}")

    if _sleeping():
        raise AssertionError("the body of the running call is stopped with the service")
