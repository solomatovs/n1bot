"""Пакет собран и импортируется; вход `boba/cli/app.py`, запущенный файлом
(так его собирает бинарник), не заслоняет своими соседями SDK `acp`."""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import boba.cli.app


class TestPackage:
    def test_imports(self) -> None:
        module = importlib.import_module("boba.cli")

        assert module.__doc__

    def test_entry_run_as_a_file_starts(self) -> None:
        entry = Path(boba.cli.app.__file__).resolve()

        done = subprocess.run(
            [sys.executable, str(entry), "--help"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

        assert done.returncode == 0, done.stderr[-2000:]
        assert not (entry.parent / "acp.py").exists()
