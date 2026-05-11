"""DTO файлового workspace-адаптера: WorkspaceLayout."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from boba.schema.coercion import ParseString

__all__ = ["WorkspaceLayout"]


@dataclass(frozen=True)
class WorkspaceLayout:
    """Раскладка namespace'ов workspace'а относительно base_dir."""

    base_dir: Annotated[
        str, "Корневая директория всех workspace-namespace'ов.", ParseString(),
    ] = "./workspaces"
    user_subdir: Annotated[
        str, "Имя поддиректории user-workspace'а внутри base_dir.", ParseString(),
    ] = "user"
    system_subdir: Annotated[
        str, "Имя поддиректории system-workspace'а внутри base_dir.", ParseString(),
    ] = "system"
    tmp_subdir: Annotated[
        str, "Имя поддиректории tmp-workspace'а внутри base_dir.", ParseString(),
    ] = "tmp"

    def root(self) -> Path:
        return Path(self.base_dir)
