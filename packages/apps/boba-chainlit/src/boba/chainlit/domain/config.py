"""Модели настроек, которые читают нижние слои.

Живут ниже сборки приложения: хранилище и доступ к инструментам не должны
зависеть от инфраструктуры.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from boba.sandbox import WorkspaceSpec
from boba.workspace.binaries import TrustedBinaries
from boba.workspace.launcher import MountingConfig

__all__ = ["LocalStorageConfig"]


class LocalStorageConfig(BaseModel):
    """Хранилище вложений (реализация BaseStorageClient)."""

    model_config = ConfigDict(extra="ignore")

    kind: Literal["local", "image"] = Field(
        default="local",
        description="local — файлы на диске; image — внутри per-thread ext4-образа.",
    )
    files_dir: str = Field(
        default="",
        description=(
            "Корневая папка на диске для файлов вложений "
            "(<files_dir>/<object_key>); обязательна при kind=local."
        ),
    )
    public_prefix: str = Field(
        default="/upload",
        description="URL-префикс serve-роута; из него собирается url элемента.",
    )
    workspace: WorkspaceSpec | None = Field(
        default=None,
        description=(
            "kind=image: рабочий каталог чата — та же запись, что у песочницы "
            "(${sandbox.workspace}). Хранилище кладёт вложения в образ "
            "пользователя, а инструмент видит их по точке монтирования."
        ),
    )
    op_timeout_sec: int = Field(
        default=60,
        ge=1,
        description="kind=image: таймаут одной операции с образом, сек.",
    )
    mounting: MountingConfig = Field(
        description=(
            "kind=image: тайминги и размеры операций монтирования — та же "
            "запись, что у песочницы (${sandbox.mounting})."
        ),
    )
    mount_dir: str = Field(
        min_length=1,
        description=(
            "kind=image: каталог, куда хранилище монтирует образ пользователя "
            "на время операции. Поверх него кладётся tmpfs, поэтому на хосте "
            "не остаётся ни точки монтирования, ни пустых каталогов."
        ),
    )
    binaries: TrustedBinaries = Field(
        description=(
            "kind=image: каталоги, откуда берутся bwrap и fuse2fs; "
            "$PATH не используется."
        ),
    )

    @model_validator(mode="after")
    def _validate_kind(self) -> Self:
        if self.kind == "local" and not self.files_dir:
            msg = (
                "section [storage] with kind=local expects files_dir as a "
                f"non-empty path, got {self.files_dir!r}"
            )
            raise ValueError(msg)

        if self.kind == "image" and self.workspace is None:
            msg = "section [storage] with kind=image expects the workspace record"
            raise ValueError(msg)

        return self
