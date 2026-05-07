"""Файловые реализации WorkspaceRegistry."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from threading import Lock
from typing import Generic, TypeVar

from boba.workspace.fs.shell import (
    FsHistoryWorkspaceShell,
    FsProjectWorkspaceShell,
    FsPromptWorkspaceShell,
    FsScratchWorkspaceShell,
    FsWorkspaceShell,
)
from boba.patterns import Id
from boba.workspace.contract import (
    HistoryWorkspaceRegistry,
    ProjectWorkspaceRegistry,
    PromptWorkspaceId,
    PromptWorkspaceRegistry,
    ScratchWorkspaceRegistry,
    WorkspaceId,
    WorkspaceNotFoundError,
    WorkspaceRegistry,
)

TWsId = TypeVar("TWsId", bound=Id)
TWs = TypeVar("TWs", bound=FsWorkspaceShell)


class FsWorkspaceRegistry(WorkspaceRegistry[TWsId], Generic[TWs, TWsId]):
    """Обобщённая файловая реализация реестра."""

    def __init__(
        self,
        base_dir: Path,
        shell_cls: type[TWs],
        subdir: str,
        id_factory: Callable[[], TWsId],
    ) -> None:
        self._base_dir = base_dir
        self._shell_cls = shell_cls
        self._subdir = subdir
        self._id_factory = id_factory
        self._lock = Lock()
        self._shells: dict[TWsId, TWs] = {}
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def create(self) -> TWs:
        with self._lock:
            ws_id = self._id_factory()
            return self._instantiate(ws_id)

    def get(self, workspace_id: TWsId) -> TWs:
        with self._lock:
            cached = self._shells.get(workspace_id)
            if cached is not None:
                return cached

            path = self._workspace_dir(workspace_id)
            if not path.is_dir():
                raise WorkspaceNotFoundError(str(path))

            shell = self._shell_cls(workspace_id, path)
            self._shells[workspace_id] = shell
            return shell

    def get_or_create(self, workspace_id: TWsId) -> TWs:
        with self._lock:
            cached = self._shells.get(workspace_id)
            if cached is not None:
                return cached
            return self._instantiate(workspace_id)

    def delete(self, workspace_id: TWsId) -> None:
        with self._lock:
            path = self._workspace_dir(workspace_id)
            if path.is_dir():
                shutil.rmtree(path)
            self._shells.pop(workspace_id, None)

    def _instantiate(self, workspace_id: TWsId) -> TWs:
        path = self._workspace_dir(workspace_id)
        path.mkdir(parents=True, exist_ok=True)
        shell = self._shell_cls(workspace_id, path)
        self._shells[workspace_id] = shell
        return shell

    def _workspace_dir(self, workspace_id: TWsId) -> Path:
        return self._base_dir / str(workspace_id.name) / self._subdir


class FsProjectWorkspaceRegistry(
    FsWorkspaceRegistry[FsProjectWorkspaceShell, WorkspaceId], ProjectWorkspaceRegistry
):
    def __init__(self, base_dir: Path, subdir: str) -> None:
        super().__init__(base_dir, FsProjectWorkspaceShell, subdir, WorkspaceId.new)


class FsHistoryWorkspaceRegistry(
    FsWorkspaceRegistry[FsHistoryWorkspaceShell, WorkspaceId], HistoryWorkspaceRegistry
):
    def __init__(self, base_dir: Path, subdir: str) -> None:
        super().__init__(base_dir, FsHistoryWorkspaceShell, subdir, WorkspaceId.new)


class FsScratchWorkspaceRegistry(
    FsWorkspaceRegistry[FsScratchWorkspaceShell, WorkspaceId], ScratchWorkspaceRegistry
):
    def __init__(self, base_dir: Path, subdir: str) -> None:
        super().__init__(base_dir, FsScratchWorkspaceShell, subdir, WorkspaceId.new)


class FsPromptWorkspaceRegistry(
    FsWorkspaceRegistry[FsPromptWorkspaceShell, PromptWorkspaceId],
    PromptWorkspaceRegistry,
):
    """Singleton-registry prompt-namespace; корень shell'а — сам root."""

    _SINGLETON_ID = PromptWorkspaceId("prompts")

    def __init__(self, root: Path) -> None:
        super().__init__(
            base_dir=root,
            shell_cls=FsPromptWorkspaceShell,
            subdir="",
            id_factory=lambda: self._SINGLETON_ID,
        )

    def _workspace_dir(self, workspace_id: PromptWorkspaceId) -> Path:
        return self._base_dir
