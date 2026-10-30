"""Сервер не импортирует код плагинов: процесс boba-mcp поднимается, получает
манифесты от программ плагинов и слушает, а ни одного модуля boba.tool.* в нём
так и не появляется — ни при процессном запуске тел, ни в песочнице.

pytest -m integration.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest
from mcp_import_probe import PluginImports

from boba.db.postgres.connection import PostgresConfig
from boba.stand.database import TestDatabase
from boba.stand.ui.database import run_blocking
from boba.stand.ui.stand import REPO_ROOT, ServiceStand, StandPorts

pytestmark = pytest.mark.integration

PROBE = Path(__file__).resolve().parent / "mcp_import_probe.py"
BOOT_SEC = 180.0
LISTENS = "boba-mcp listens on"
PLUGINS = ("bash", "doc", "pg", "ch", "web")


class ProbedService:
    """Процесс сервиса под пробой импорта: старт до строки готовности в
    логе, остановка сигналом; лог — для проверки."""

    def __init__(self, config: ServiceStand, log_path: Path) -> None:
        self._config = config
        self._log_path = log_path

    def run(self) -> str:
        """Поднять сервис, дождаться готовности, остановить; отдать лог."""
        self._config.write()
        command = [
            sys.executable,
            str(PROBE),
            *self._config.files.arguments(self._config.app),
        ]
        with self._log_path.open("wb") as log:
            process = subprocess.Popen(
                command,
                env=self._config.env(),
                cwd=str(REPO_ROOT),
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                self._await_ready(process)
            finally:
                process.terminate()
                process.wait(timeout=30)

        return self._log()

    def _await_ready(self, process: subprocess.Popen[bytes]) -> None:
        deadline = time.monotonic() + BOOT_SEC
        while LISTENS not in self._log():
            if process.poll() is not None:
                msg = f"boba-mcp exited with {process.returncode}:\n{self._tail()}"
                raise AssertionError(msg)

            if time.monotonic() > deadline:
                msg = f"boba-mcp did not listen in {BOOT_SEC}s:\n{self._tail()}"
                raise AssertionError(msg)

            time.sleep(0.2)

    def _log(self) -> str:
        if not self._log_path.is_file():
            return ""

        return self._log_path.read_text(encoding="utf-8", errors="replace")

    def _tail(self) -> str:
        return "\n".join(self._log().splitlines()[-40:])


@pytest.mark.parametrize("sandbox", [False, True])
def test_service_never_imports_plugin_code(
    tmp_path: Path, stand_postgres: PostgresConfig, sandbox: bool
) -> None:
    config = ServiceStand(
        workdir=tmp_path,
        app_port=StandPorts().free(),
        db_name=run_blocking(TestDatabase.ensure(stand_postgres)),
        sandbox=sandbox,
        plugins=PLUGINS,
    )

    log = ProbedService(config, tmp_path / "mcp.log").run()

    imported: list[str] = []
    for line in log.splitlines():
        if PluginImports.MARK in line:
            imported.append(line)

    assert not imported, "\n".join(imported)


def test_probe_sees_a_plugin_import() -> None:
    """Проба не слепа: импорт кода плагина в процессе под хуком она видит."""
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(PROBE.parent)!r})\n"
        "from mcp_import_probe import PluginImports\n"
        "sys.addaudithook(PluginImports())\n"
        "import boba.tool.pg.plugin\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        timeout=120,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert f"{PluginImports.MARK}boba.tool.pg.plugin" in done.stderr.decode()
