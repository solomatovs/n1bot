"""Модели настроек, которые читает слой данных.

Живут ниже сборки приложения: хранилище не должно зависеть от инфраструктуры.
"""

from __future__ import annotations

import os
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from boba.toolkit.binaries import TrustedBinaries
from boba.workspace.launcher import LauncherConfig

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
    image_path: str = Field(
        default="",
        description=(
            "kind=image: шаблон пути образа с {user_id}/{thread_id}, "
            'например ".../workspace/{user_id}/{thread_id}.ext4".'
        ),
    )
    image_template: str = Field(
        default="",
        description="kind=image: шаблонный ext4-образ для первого обращения.",
    )
    op_timeout_sec: int = Field(
        default=60,
        ge=1,
        description="kind=image: таймаут одной операции с образом, сек.",
    )
    launcher: LauncherConfig = Field(
        description="kind=image: тайминги и размеры операций лаунчера образов.",
    )
    binaries: TrustedBinaries = Field(
        description=(
            "kind=image: каталоги, откуда берутся bwrap и fuse2fs; "
            "$PATH не используется."
        ),
    )

    @field_validator("image_path", "image_template", mode="after")
    @classmethod
    def _canonicalize(cls, value: str) -> str:
        """bwrap не примет относительный путь: корень песочницы read-only."""
        if not value:
            return value
        return os.path.normpath(os.path.abspath(os.path.expanduser(value)))

    @model_validator(mode="after")
    def _validate_kind(self) -> Self:
        if self.kind == "local" and not self.files_dir:
            msg = "storage: kind=local requires files_dir"
            raise ValueError(msg)
        if self.kind == "image" and not (self.image_path and self.image_template):
            msg = "storage: kind=image requires image_path and image_template"
            raise ValueError(msg)
        return self
