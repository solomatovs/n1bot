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

from boba.workspace.contract import ProjectWorkspaceShell


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> ProjectWorkspaceShell:
    """`ProjectWorkspaceShell` поверх tmp_path (реальная FS-реализация)."""
    from boba.agent.workspace_fs.shell import FsProjectWorkspaceShell

    return FsProjectWorkspaceShell(workspace_id="test", root=tmp_path)  # type: ignore[arg-type]
