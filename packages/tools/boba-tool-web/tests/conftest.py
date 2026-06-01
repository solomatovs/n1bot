"""Pytest-фикстуры пакета boba-tool-web.

`tmp_workspace` — реальный `WorkspaceShell` поверх `tmp_path`. Для tool-
тестов нам нужен настоящий FS-shell, потому что и `web_download`, и
`web_fetch` дёргают `mkdir`/`atomic_write_binary`/`read_lines` — мокать
их каждый раз дороже, чем включить готовую in-memory-реализацию.

Конкретный класс берётся из `boba.transport.fs` (рабочая реализация
`WorkspaceShell`, ту же что использует прод).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from boba.tool.web.tools.grep import WebGrepConfig
from boba.workspace.contract import WorkspaceShell


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> WorkspaceShell:
    """`WorkspaceShell` поверх tmp_path (реальная FS-реализация)."""
    from boba.agent.workspace_fs.shell import FsWorkspaceShell

    return FsWorkspaceShell(workspace_id="test", root=tmp_path)  # type: ignore[arg-type]


@pytest.fixture
def web_grep_cfg() -> WebGrepConfig:
    """`WebGrepConfig` из живого `$BOBA_CONFIG_PATH` (integration-режим).

    Skip, если `[tool.web]`/профили не сконфигурированы — тест требует
    реального whitelist'а хостов (raw.githubusercontent.com, cwiki, ...).
    """
    try:
        return WebGrepConfig()  # type: ignore[call-arg]
    except ValidationError as e:
        pytest.skip(f"[tool.web.grep] не сконфигурирован: {e}")
