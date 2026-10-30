"""Контейнер тестовых MCP-серверов слота прогона на сети стендов.

Ошибки:
StandError — образа нет, контейнер не запустился либо его серверы не начали
    слушать.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from enum import IntEnum, StrEnum
from typing import ClassVar

from boba.stand.site import StandError


class AppServer(IntEnum):
    """Серверы MCP контейнера boba-testapps по портам, на пути /mcp: примеры
    MCP Apps из npm (2025-11-25, без сессий), свой сервер на TypeScript SDK
    1.x с сессиями в четырёх редакциях — LEGACY знает все версии
    рукопожатия SDK, OLDER только 2025-06-18, DUAL — две эпохи более поздней
    редакции (2027-01-01 с конвертом и рукопожатие 2025-06-18, 2025-03-26),
    AHEAD — только 2027-01-01 без рукопожатия — и серверы fastmcp[apps]:
    PREFAB (инструменты с видами Prefab и свой HTML), NOTES (FastMCPApp со
    входом для модели и backend для приложения), GENUI (провайдер
    генерации)."""

    SEGMENTS = 3101
    DEBUG = 3105
    LEGACY = 3110
    OLDER = 3111
    DUAL = 3112
    AHEAD = 3113
    PREFAB = 3120
    NOTES = 3121
    GENUI = 3122


class AppStatic(IntEnum):
    """Статика контейнера boba-testapps по портам: RENDERER — dist рендерера
    Prefab (@prefecthq/prefab-ui), который серверы fastmcp[apps] называют в
    своих ресурсах ui:// вместо CDN."""

    RENDERER = 3124


class AppsDocker(StrEnum):
    """Запуск контейнера boba-testapps: docker, образ, имя по процессу
    теста, сеть, метка тестовых контейнеров (их не трогает autoheal) и
    метка процесса-владельца."""

    DOCKER = "/usr/bin/docker"
    IMAGE = "boba-testapps:latest"
    NAME = "boba-e2e-apps-{owner}"
    NETWORK = "ss-stands"
    LABEL = "boba.e2e=1"
    OWNER = "boba.e2e.apps"
    BUILD = "make -C build build APP=testapps"
    OPTION = "--testapps-image"
    OPTION_DEST = "testapps_image"


class AppsSlot:
    """Контейнер тестовых серверов MCP прежней редакции процесса pytest:
    чужие реализации, а не стенд кода boba.

    Создаётся сессионной фикстурой apps_slot на процесс: наборы и процессы
    xdist идут параллельно, поэтому контейнер у каждого свой, с номером
    процесса в имени и метке. start() снимает контейнеры умерших процессов,
    запускает образ boba-testapps на сети стендов и ждёт, пока все серверы
    слушают; тесты ходят на адрес контейнера (host) напрямую, как браузер и
    клиент на хосте. stdio() — тот же свой сервер отдельным контейнером без
    сети по stdio. stop() снимает контейнер.
    """

    BOOT_SEC: ClassVar[float] = 60.0
    PATH: ClassVar[str] = "/mcp"

    def __init__(self, image: str) -> None:
        self._image = image
        self._owner = os.getpid()
        self._name = AppsDocker.NAME.format(owner=self._owner)
        self.host = ""

    def start(self) -> None:
        """Ошибки:
        StandError — образа нет, контейнер не запустился или не слушает.
        """
        self._sweep()
        run = (
            "run",
            "-d",
            "--rm",
            "--name",
            self._name,
            "--network",
            AppsDocker.NETWORK,
            "--label",
            AppsDocker.LABEL,
            "--label",
            f"{AppsDocker.OWNER}={self._owner}",
            self._image,
        )
        started = self._docker(*run)
        if started.returncode != 0:
            msg = (
                f"apps slot {self._name}: docker run {self._image} failed: "
                f"{started.stderr.strip()}; build the image with {AppsDocker.BUILD}"
            )
            raise StandError(msg)

        inspected = self._docker(
            "inspect",
            "-f",
            "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
            self._name,
        )
        self.host = inspected.stdout.strip()
        for server in AppServer:
            self._listening(server)

        for static in AppStatic:
            self._listening(static)

    def stop(self) -> None:
        self._docker("rm", "-f", self._name)

    def renderer_url(self) -> str:
        """Источник рендерера Prefab, с которого серверы fastmcp[apps] велят
        браузеру грузить renderer.js и renderer.css."""
        return f"http://{self.host}:{AppStatic.RENDERER.value}"

    def stdio(self) -> tuple[str, ...]:
        """Команда своего сервера по stdio: контейнер без сети на вызов."""
        return (
            AppsDocker.DOCKER,
            "run",
            "-i",
            "--rm",
            "--network",
            "none",
            "--label",
            AppsDocker.LABEL,
            "--entrypoint",
            "node",
            self._image,
            "legacy-server.mjs",
            "--stdio",
        )

    def _sweep(self) -> None:
        """Снимает контейнеры процессов, которые умерли, не сняв их."""
        listed = self._docker(
            "ps",
            "-a",
            "--filter",
            f"label={AppsDocker.OWNER}",
            "--format",
            f'{{{{.Names}}}} {{{{.Label "{AppsDocker.OWNER}"}}}}',
        )
        for line in listed.stdout.splitlines():
            name, _, owner = line.partition(" ")
            if self._alive(int(owner)):
                continue

            self._docker("rm", "-f", name)

    @staticmethod
    def _alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

        return True

    def _listening(self, server: AppServer | AppStatic) -> None:
        deadline = time.monotonic() + self.BOOT_SEC
        while True:
            try:
                with socket.create_connection((self.host, server.value), 1.0):
                    return
            except OSError as exc:
                if time.monotonic() > deadline:
                    logs = self._docker("logs", "--tail", "40", self._name)
                    msg = (
                        f"apps slot {self._name}: server {server.name} is not "
                        f"listening on {self.host}:{server.value} within "
                        f"{self.BOOT_SEC}s: {exc}; container log: "
                        f"{logs.stdout}{logs.stderr}"
                    )
                    raise StandError(msg) from exc

                time.sleep(0.2)

    def _docker(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 — docker по абсолютному пути, аргументы свои
            [AppsDocker.DOCKER, *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=self.BOOT_SEC,
        )
