"""Запуск чата скриптом: каталог `boba/chainlit` встаёт первым в `sys.path`,
и ни один его пакет не должен заслонить модуль верхнего уровня, который код
чата импортирует (SDK `acp`, `mcp`, `chainlit`)."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import ClassVar

import pytest

import boba.chainlit

pytestmark = pytest.mark.anyio


class ScriptLaunch:
    """Интерпретатор с каталогом пакета чата первым в `sys.path`, как при
    запуске `python …/boba/chainlit/main.py`."""

    PROBE: ClassVar[str] = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "import boba.chainlit.infra.providers; import acp; print(acp.__file__)"
    )
    TIMEOUT_SEC: ClassVar[float] = 120.0

    def __init__(self) -> None:
        self.package_dir = Path(boba.chainlit.__path__[0]).resolve()

    def imported(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", self.PROBE, str(self.package_dir)],
            capture_output=True,
            text=True,
            timeout=self.TIMEOUT_SEC,
            check=False,
        )


class TestScriptLaunch:
    async def test_package_dir_first_in_path_keeps_sdk_modules(self) -> None:
        launch = ScriptLaunch()

        done = await asyncio.to_thread(launch.imported)

        assert done.returncode == 0, done.stderr[-2000:]
        sdk = Path(done.stdout.strip()).resolve()
        assert launch.package_dir not in sdk.parents, sdk

    async def test_no_package_of_the_chat_is_named_as_an_sdk_module(self) -> None:
        launch = ScriptLaunch()
        names: list[str] = []
        for entry in launch.package_dir.iterdir():
            names.append(entry.stem)

        assert "acp" not in names
        assert "mcp" not in names
        assert "chainlit" not in names
