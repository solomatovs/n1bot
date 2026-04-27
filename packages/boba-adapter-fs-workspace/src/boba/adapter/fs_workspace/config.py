"""Конфиг-секция файлового workspace-адаптера."""

from __future__ import annotations

from typing import ClassVar

from boba.domain.config import WorkspaceLayout
from boba.domain.core.config import ConfigSection, FieldSpec, ObjectSchema
from boba.domain.core.patterns import StrId
from boba.domain.core.validators import ChainConverter, Default, ParseString


class WorkspacesSection(ConfigSection[WorkspaceLayout]):
    """Раскладка namespace'ов workspace'а относительно base_dir."""

    id: ClassVar[StrId] = StrId("workspaces")
    namespace: ClassVar[tuple[str, ...]] = ("workspaces",)

    schema: ClassVar[ObjectSchema[WorkspaceLayout]] = ObjectSchema(
        description="Раскладка namespace'ов workspace'а относительно "
        "base_dir.",
        fields=[
            FieldSpec(
                name="base_dir",
                converter=ChainConverter(Default("./workspaces"), ParseString()),
                description="Корневая директория всех workspace-namespace'ов.",
            ),
            FieldSpec(
                name="user_subdir",
                converter=ChainConverter(Default("user"), ParseString()),
                description="Имя поддиректории user-workspace'а внутри base_dir.",
            ),
            FieldSpec(
                name="system_subdir",
                converter=ChainConverter(Default("system"), ParseString()),
                description="Имя поддиректории system-workspace'а внутри base_dir.",
            ),
            FieldSpec(
                name="tmp_subdir",
                converter=ChainConverter(Default("tmp"), ParseString()),
                description="Имя поддиректории tmp-workspace'а внутри base_dir.",
            ),
        ],
        factory=WorkspaceLayout,
    )
