"""DTO файлового workspace-адаптера: WorkspaceLayout + SCHEMA."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from boba.coercion import ChainCoercer, Default, ParseString
from boba.declaration import FieldSpec, ObjectSchema

__all__ = ["WorkspaceLayout"]


@dataclass(frozen=True)
class WorkspaceLayout:
    """Раскладка namespace'ов workspace'а относительно base_dir."""

    base_dir: str = "./workspaces"
    user_subdir: str = "user"
    system_subdir: str = "system"
    tmp_subdir: str = "tmp"

    SCHEMA: ClassVar[ObjectSchema[WorkspaceLayout]]

    def root(self) -> Path:
        return Path(self.base_dir)


WorkspaceLayout.SCHEMA = ObjectSchema(
    description="Раскладка namespace'ов workspace'а относительно base_dir.",
    fields=[
        FieldSpec(
            name="base_dir",
            coercer=ChainCoercer(Default("./workspaces"), ParseString()),
            description="Корневая директория всех workspace-namespace'ов.",
        ),
        FieldSpec(
            name="user_subdir",
            coercer=ChainCoercer(Default("user"), ParseString()),
            description="Имя поддиректории user-workspace'а внутри base_dir.",
        ),
        FieldSpec(
            name="system_subdir",
            coercer=ChainCoercer(Default("system"), ParseString()),
            description="Имя поддиректории system-workspace'а внутри base_dir.",
        ),
        FieldSpec(
            name="tmp_subdir",
            coercer=ChainCoercer(Default("tmp"), ParseString()),
            description="Имя поддиректории tmp-workspace'а внутри base_dir.",
        ),
    ],
    factory=WorkspaceLayout,
)
