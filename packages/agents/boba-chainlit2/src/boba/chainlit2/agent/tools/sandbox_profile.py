"""SandboxProfile: декларативное описание песочницы под bubblewrap."""

from __future__ import annotations

import os

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = ["SandboxProfile"]


_DEFAULT_RO_BINDS: tuple[str, ...] = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/etc/alternatives",
    "/etc/resolv.conf",
)


class SandboxProfile(BaseModel):
    """Параметры одной песочницы; LLM выбирает профиль по имени."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rootfs: str = Field(
        default="",
        description=(
            "Каталог, монтируемый read-only как / песочницы; ro_binds "
            "ложатся поверх. Пустая строка — корень не монтируется. "
            "Собирается build/scripts/make-sandbox-rootfs.sh."
        ),
    )
    ro_binds: tuple[str, ...] = Field(
        default=_DEFAULT_RO_BINDS,
        description="Host-пути read-only; несуществующие пропускаются.",
    )
    rw_binds: tuple[str, ...] = Field(
        default=(),
        description="Host-пути read-write; workspace_root добавляется сам.",
    )
    tmpfs: tuple[str, ...] = Field(
        default=("/tmp",),  # noqa: S108 — внутри песочницы, не host
        description="Mountpoints под tmpfs (in-memory).",
    )
    network: bool = Field(
        default=False,
        description="False — `--unshare-net` (нет сети). True — сеть хоста.",
    )
    env_set: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Env внутри песочницы; host-env не наследуется. Для запуска "
            "утилит обычно нужен 'PATH'."
        ),
    )
    timeout_sec: int = Field(
        default=30,
        ge=1,
        le=3600,
        description="Жёсткий таймаут выполнения процесса (1..3600 сек).",
    )
    max_output_bytes: int = Field(
        default=256 * 1024,
        ge=1024,
        description="Лимит stdout И stderr по отдельности; сверх — обрезка.",
    )
    cwd: str = Field(
        default="",
        description="Рабочая директория; пустая = workspace_root плагина.",
    )

    @field_validator("rootfs", mode="after")
    @classmethod
    def _canonicalize_rootfs(cls, value: str) -> str:
        if not value:
            return value
        return os.path.normpath(os.path.abspath(os.path.expanduser(value)))

    @field_validator("ro_binds", "rw_binds", "tmpfs", mode="after")
    @classmethod
    def _canonicalize_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Абсолютный нормализованный путь; симлинки разыменовывает builder."""
        return tuple(
            os.path.normpath(os.path.abspath(os.path.expanduser(p))) for p in value
        )
