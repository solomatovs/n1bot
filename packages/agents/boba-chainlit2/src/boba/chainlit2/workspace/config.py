"""Pydantic-обёртка LauncherOptions: toml-секция, лаунчеру уходит CLI-флагами."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from boba.chainlit2.workspace.options import LauncherOptions

__all__ = ["LauncherConfig"]


class LauncherConfig(BaseModel):
    """Тайминги и размеры операций лаунчера образов."""

    model_config = ConfigDict(extra="ignore")

    _defaults: ClassVar[LauncherOptions] = LauncherOptions()

    mount_wait_sec: float = Field(
        default=_defaults.mount_wait_sec,
        gt=0,
        description="Сколько ждать появления fuse-монтирования, сек.",
    )
    mount_poll_sec: float = Field(
        default=_defaults.mount_poll_sec,
        gt=0,
        description="Период опроса mountinfo при ожидании монтирования, сек.",
    )
    shutdown_wait_sec: float = Field(
        default=_defaults.shutdown_wait_sec,
        gt=0,
        description="Сколько ждать штатного выхода fuse2fs после SIGTERM, сек.",
    )
    copy_chunk_bytes: int = Field(
        default=_defaults.copy_chunk_bytes,
        gt=0,
        description="Размер блока sparse-копирования шаблонного образа, байт.",
    )

    def to_options(self) -> LauncherOptions:
        return LauncherOptions(
            mount_wait_sec=self.mount_wait_sec,
            mount_poll_sec=self.mount_poll_sec,
            shutdown_wait_sec=self.shutdown_wait_sec,
            copy_chunk_bytes=self.copy_chunk_bytes,
        )
