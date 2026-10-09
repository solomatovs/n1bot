"""Сорванный старт приложения: postgres принимает соединение и молчит.

Процесс приложения обязан выйти сам с ненулевым кодом: перезапускает его
политика рестарта контейнера, а живой процесс без приложения она не видит.
Молчащий адрес держит воркер пула посреди подключения — именно в этом
состоянии незакрытый пул не давал циклу событий закрыться.

pytest -m integration.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import pytest

from boba.stand.ui.stand import REPO_ROOT, StandApp, StandConfig, free_port
from boba.stand.ui.toml_text import TomlText

pytestmark = pytest.mark.integration

EXIT_SEC = 90.0
"""Срок выхода: импорты приложения, таймаут пула и закрытие пула с запасом."""


class SilentPostgres:
    """Адрес вместо postgres: ядро принимает TCP-соединение в очередь, а на
    стартовый пакет никто не отвечает, и подключение висит до своего таймаута."""

    HOST: ClassVar[str] = "127.0.0.1"

    def __init__(self) -> None:
        self._socket = socket.socket()
        self._socket.bind((self.HOST, 0))
        self._socket.listen()

    @property
    def port(self) -> int:
        return int(self._socket.getsockname()[1])

    def close(self) -> None:
        self._socket.close()


@dataclass(kw_only=True)
class SilentDatabaseStand(StandConfig):
    """Конфиг стенда, у которого адрес postgres площадки уведён на молчащий."""

    postgres: SilentPostgres

    def write(self) -> Path:
        path = super().write()
        with path.open("rb") as handle:
            doc: dict[str, Any] = tomllib.load(handle)

        doc["site"]["pg_addr"] = self.postgres.HOST
        doc["site"]["pg_port"] = self.postgres.port
        path.write_text(TomlText.dumps(doc), encoding="utf-8")

        return path


@pytest.fixture
def silent_postgres() -> Iterator[SilentPostgres]:
    postgres = SilentPostgres()
    try:
        yield postgres
    finally:
        postgres.close()


@pytest.mark.parametrize("app", [StandApp.CHAINLIT, StandApp.STUDIO])
def test_startup_without_postgres_exits_with_a_failure(
    app: StandApp, tmp_path: Path, silent_postgres: SilentPostgres
) -> None:
    config = SilentDatabaseStand(
        workdir=tmp_path,
        app=app,
        app_port=free_port(),
        llm_port=free_port(),
        db_name="unreachable",
        postgres=silent_postgres,
    )
    config.write()
    log_path = tmp_path / "app.log"
    command = [sys.executable, "-m", app.module, "--config", str(config.config_path)]

    with log_path.open("wb") as log:
        process = subprocess.Popen(
            command,
            env=config.env(),
            cwd=str(REPO_ROOT),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        started = time.monotonic()
        try:
            code = process.wait(timeout=EXIT_SEC)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.wait()
            tail = "\n".join(log_path.read_text(errors="replace").splitlines()[-40:])
            msg = (
                f"{app.value} did not exit in {EXIT_SEC}s after a failed startup "
                f"with a silent postgres:\n{tail}"
            )
            raise AssertionError(msg) from exc

    elapsed = time.monotonic() - started
    output = log_path.read_text(encoding="utf-8", errors="replace")
    tail = "\n".join(output.splitlines()[-40:])
    if "Application startup failed" not in output:
        raise AssertionError(
            f"{app.value} is expected to fail its startup on postgres, exited with "
            f"{code} in {elapsed:.1f}s:\n{tail}"
        )

    if code == 0:
        raise AssertionError(
            f"{app.value} exits with a failure code after a failed startup, got 0 "
            f"in {elapsed:.1f}s:\n{tail}"
        )
