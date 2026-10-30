"""Режимы командной строки `boba` настоящим процессом: без режима —
отказ интерактивного режима с подсказкой и кодом 2; --stdio не сочетается
с подкомандой; прежней подкоманды acp нет; справка называет режимы."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import ClassVar


class BobaRun:
    """Запуск `python -m boba.cli.app` с аргументами в пустом каталоге."""

    TIMEOUT_SEC: ClassVar[float] = 120.0

    def __init__(self, cwd: Path) -> None:
        self._cwd = cwd

    def run(self, *argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "boba.cli.app", *argv],
            capture_output=True,
            text=True,
            timeout=self.TIMEOUT_SEC,
            check=False,
            cwd=self._cwd,
            stdin=subprocess.DEVNULL,
        )


class TestModes:
    def test_without_a_mode_the_interactive_mode_is_not_ready(
        self, tmp_path: Path
    ) -> None:
        done = BobaRun(tmp_path).run()

        assert done.returncode == 2
        assert done.stdout == ""
        assert "the interactive mode is not available yet" in done.stderr
        assert "boba --stdio" in done.stderr
        assert "boba serve --config FILE [--config FILE]" in done.stderr

    def test_stdio_does_not_combine_with_a_command(self, tmp_path: Path) -> None:
        done = BobaRun(tmp_path).run("--stdio", "settings-schema")

        assert done.returncode == 2
        assert "does not combine with the command 'settings-schema'" in done.stderr

    def test_the_former_acp_command_is_unknown(self, tmp_path: Path) -> None:
        done = BobaRun(tmp_path).run("acp")

        assert done.returncode == 2
        assert "invalid choice: 'acp'" in done.stderr

    def test_help_names_the_modes(self, tmp_path: Path) -> None:
        done = BobaRun(tmp_path).run("--help")

        assert done.returncode == 0
        assert "--stdio" in done.stdout
        assert "--settings" in done.stdout
        assert "{serve,health,self-check,settings-schema}" in done.stdout
