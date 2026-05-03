"""Конфиг-секция файлового workspace-адаптера."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from boba.config.section import ConfigSection
from boba.declaration import FieldSpec, ObjectSchema
from boba.patterns import StrId
from boba.coercion import ChainCoercer, Default, ParseString


@dataclass(frozen=True)
class WorkspaceLayout:
    """Раскладка namespace'ов workspace'а относительно base_dir."""

    base_dir: str = "./workspaces"
    user_subdir: str = "user"
    system_subdir: str = "system"
    tmp_subdir: str = "tmp"

    def root(self) -> Path:
        return Path(self.base_dir)


class WorkspacesSection(ConfigSection[WorkspaceLayout]):
    """Раскладка namespace'ов workspace'а относительно base_dir."""

    id: ClassVar[StrId] = StrId("workspaces")
    namespace: ClassVar[tuple[str, ...]] = ("workspaces",)

    schema: ClassVar[ObjectSchema[WorkspaceLayout]] = ObjectSchema(
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
