"""Запуск приложения стенда и жизненный цикл процесса.

Конфигурация находится в ui.config, подготовка базы — в ui.database.
Имена конфигурации переэкспортированы для существующих потребителей стенда.

Ошибки:
StandError — приложение не запустилось или не завершилось в отведённое время.
"""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import ClassVar, Generic

import httpx
from typing_extensions import TypeVar

from boba.stand.ui.config import (
    REPO_ROOT,
    RuntimeLayout,
    ServerSeed,
    ServerSeedSpec,
    ServiceStand,
    StandAgentAuth,
    StandApp,
    StandAppTraits,
    StandAuth,
    StandConfig,
    StandCredential,
    StandEnv,
    StandError,
    StandFiles,
    StandOverlay,
    StandPlugins,
    StandPorts,
    StandRuntime,
    StandServerName,
    StandService,
    StandToolNames,
    StandUrl,
)
from boba.stand.ui.database import StandServerSeed, seed_service_roles

__all__ = [
    "REPO_ROOT",
    "RuntimeLayout",
    "ServerSeed",
    "ServerSeedSpec",
    "ServiceStand",
    "StandAgentAuth",
    "StandApp",
    "StandAppTraits",
    "StandAuth",
    "StandConfig",
    "StandCredential",
    "StandEnv",
    "StandError",
    "StandFiles",
    "StandOverlay",
    "StandPlugins",
    "StandPorts",
    "StandProcess",
    "StandRuntime",
    "StandServerName",
    "StandService",
    "StandToolNames",
    "StandUrl",
]

ConfigT = TypeVar("ConfigT", bound=StandService, default=StandConfig)


@dataclass
class StandProcess(Generic[ConfigT]):
    """Процесс приложения стенда: живёт на время сессии тестов."""

    config: ConfigT
    log_path: Path
    process: subprocess.Popen[bytes] | None = None
    companion: StandProcess[StandService] | None = None
    """Процесс приложения-спутника: поднимается раньше, гаснет позже."""

    COMPLAINT_MARKERS: ClassVar[tuple[str, ...]] = (
        "ERROR:",
        "Traceback",
        "Task exception was never retrieved",
        "loop mismatch",
    )
    """Маркеры строк, которых в логе живого хода быть не должно."""

    _server_seed: ServerSeed | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        if not isinstance(self.config, StandConfig):
            return

        if self.config.app is not StandApp.AID:
            return

        if not self.config.seed_servers:
            return

        self._server_seed = StandServerSeed(self.config)

    def start(self, boot_timeout_sec: float) -> None:
        """Поднимает процесс; неудачный старт гасит его."""
        try:
            self._start(boot_timeout_sec)
        except Exception:
            self.stop()
            raise

    def _start(self, boot_timeout_sec: float) -> None:
        needed = self.config.companion()
        if needed is not None:
            log_path = self.log_path.with_name(f"{needed.app.value}.log")
            self.companion = StandProcess[StandService](
                config=needed, log_path=log_path
            )
            self.companion.start(boot_timeout_sec)

        self.config.write()
        if isinstance(self.config, ServiceStand):
            seed_service_roles(self.config)
        self.process = self._spawn()
        deadline = time.monotonic() + boot_timeout_sec
        path = self.config.url_prefix + self.config.app.ready_path
        self._await_ready(
            self.process, StandUrl.of(self.config.app_port, path), deadline
        )
        if self._server_seed is not None:
            self._server_seed.run()

    def _spawn(self) -> subprocess.Popen[bytes]:
        handle = self.log_path.open("wb")
        return subprocess.Popen(  # noqa: S603
            [
                sys.executable,
                "-m",
                self.config.app.module,
                *self.config.files.arguments(self.config.app),
            ],
            env=self.config.env(),
            cwd=str(REPO_ROOT),
            stdout=handle,
            stderr=subprocess.STDOUT,
        )

    def stop(self) -> None:
        self._stop_own()
        if self.companion is not None:
            self.companion.stop()

    def _stop_own(self) -> None:
        if self.process is None:
            return

        self.process.terminate()
        try:
            self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.process.kill()

    def log_lines(self) -> int:
        """Сколько строк в логе приложения сейчас: отметка начала хода."""
        if not self.log_path.is_file():
            return 0

        text = self.log_path.read_text(encoding="utf-8", errors="replace")
        return len(text.splitlines())

    def complaints(self, since_line: int = 0) -> list[str]:
        """Строки лога приложения об ошибках начиная с отметки: ход обязан
        проходить без них, а чужие ходы в общем логе не считаются."""
        if not self.log_path.is_file():
            return []

        text = self.log_path.read_text(encoding="utf-8", errors="replace")

        found: list[str] = []
        for line in text.splitlines()[since_line:]:
            for marker in self.COMPLAINT_MARKERS:
                if marker in line:
                    found.append(line)
                    break

        return found

    def tail(self, lines: int = 40) -> str:
        """Хвост лога процесса: без него падение теста молчит о причине."""
        if not self.log_path.is_file():
            return ""

        text = self.log_path.read_text(encoding="utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:])

    def _await_ready(
        self, process: subprocess.Popen[bytes], url: str, deadline: float
    ) -> None:
        app = self.config.app.value
        while time.monotonic() < deadline:
            if process.poll() is not None:
                code = process.returncode
                msg = (
                    f"{app} exited with code {code} before answering at {url}:"
                    f"\n{self.tail()}"
                )
                raise StandError(msg)

            try:
                response = httpx.get(url, timeout=2.0)
            except httpx.HTTPError:
                time.sleep(0.3)
                continue

            if response.status_code < HTTPStatus.INTERNAL_SERVER_ERROR:
                return

            time.sleep(0.3)

        msg = (
            f"GET {url}: {app} gave no reply below 500 before the start deadline:"
            f"\n{self.tail()}"
        )
        raise StandError(msg)
