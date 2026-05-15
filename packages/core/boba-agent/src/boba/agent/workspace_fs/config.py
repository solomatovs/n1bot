"""DTO файлового workspace-адаптера: WorkspaceLayout."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["WorkspaceLayout"]


class WorkspaceLayout(BaseModel):
    """Раскладка namespace'ов workspace'а относительно base_dir."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_dir: str = Field(
        default="./workspaces",
        description="Корневая директория всех workspace-namespace'ов.",
    )
    user_subdir: str = Field(
        default="user",
        description="Имя поддиректории user-workspace'а внутри base_dir.",
    )
    system_subdir: str = Field(
        default="system",
        description="Имя поддиректории system-workspace'а внутри base_dir.",
    )
    tmp_subdir: str = Field(
        default="tmp",
        description="Имя поддиректории tmp-workspace'а внутри base_dir.",
    )

    def root(self) -> Path:
        return Path(self.base_dir)
