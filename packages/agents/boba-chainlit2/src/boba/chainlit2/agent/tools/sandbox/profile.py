"""SandboxProfile: декларативное описание песочницы под bubblewrap."""

from __future__ import annotations

import os
import string
from collections.abc import Mapping
from typing import ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = ["BindSpec", "SandboxProfile", "TmpfsSpec"]


class BindSpec(BaseModel):
    """Одно монтирование `host[:target]`; без target путь внутри = host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    VARS: ClassVar[tuple[str, ...]] = ("user_id", "thread_id")

    host: str = Field(description="Host-путь; допускает {user_id}/{thread_id}.")
    target: str = Field(description="Точка монтирования внутри песочницы.")

    @classmethod
    def parse(cls, raw: str) -> Self:
        host, sep, target = raw.partition(":")
        if not host:
            msg = f"bind {raw!r}: пустой host-путь"
            raise ValueError(msg)
        host = cls._canonical(host)
        return cls(host=host, target=target if sep else host)

    @field_validator("host", "target", mode="after")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        cls.check_vars(value)
        if not value.startswith("/"):
            msg = f"bind: путь должен быть абсолютным, получено {value!r}"
            raise ValueError(msg)
        return value

    def render(self, variables: Mapping[str, str]) -> Self:
        return type(self)(
            host=self.substitute(self.host, variables),
            target=self.substitute(self.target, variables),
        )

    @staticmethod
    def check_vars(template: str) -> str:
        try:
            fields = [
                name
                for _, name, _, _ in string.Formatter().parse(template)
                if name is not None
            ]
        except ValueError as e:
            msg = f"неверный шаблон пути {template!r}: {e}"
            raise ValueError(msg) from e
        unknown = sorted(set(fields) - set(BindSpec.VARS))
        if unknown:
            msg = (
                f"неизвестные переменные {unknown} в пути {template!r} "
                f"(известны: {', '.join(BindSpec.VARS)})"
            )
            raise ValueError(msg)
        return template

    @staticmethod
    def substitute(template: str, variables: Mapping[str, str]) -> str:
        try:
            return template.format_map(dict(variables))
        except KeyError as e:
            msg = (
                f"sandbox: переменная {{{e.args[0]}}} в пути {template!r} "
                f"недоступна — нет сессии chainlit"
            )
            raise RuntimeError(msg) from e

    @staticmethod
    def _canonical(path: str) -> str:
        return os.path.normpath(os.path.abspath(os.path.expanduser(path)))


class TmpfsSpec(BaseModel):
    """Один tmpfs `dest[:size]`; size с суффиксом K/M/G, без суффикса — байты."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(description="Mountpoint внутри песочницы.")
    size_bytes: int = Field(default=0, ge=0, description="0 — без лимита.")

    @classmethod
    def parse(cls, raw: str) -> Self:
        path, sep, size = raw.partition(":")
        if not sep:
            return cls(path=path)
        return cls(path=path, size_bytes=cls._parse_size(size))

    @field_validator("path", mode="after")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        if not value.startswith("/"):
            msg = f"tmpfs: путь должен быть абсолютным, получено {value!r}"
            raise ValueError(msg)
        return os.path.normpath(value)

    @staticmethod
    def _parse_size(raw: str) -> int:
        factors = {"K": 1024, "M": 1024**2, "G": 1024**3}
        text = raw.strip().upper()
        factor = factors.get(text[-1:], 0)
        digits = text[:-1] if factor else text
        if not digits.isdigit() or int(digits) == 0:
            msg = f"tmpfs: неверный размер {raw!r} (пример: 256M, 1G)"
            raise ValueError(msg)
        return int(digits) * (factor or 1)


class SandboxProfile(BaseModel):
    """Параметры одной песочницы; LLM выбирает профиль по имени."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rootfs: str = Field(
        default="",
        description=(
            "Каталог, монтируемый read-only как / песочницы; ro_binds "
            "ложатся поверх. Пустая строка — корень не монтируется. "
            "Собирается целью make sandbox-rootfs."
        ),
    )
    ro_binds: tuple[BindSpec, ...] = Field(
        default=(),
        description=(
            "Host-пути read-only, формат `host[:target]`; несуществующие "
            "пропускаются. Рендерятся {user_id}/{thread_id}."
        ),
    )
    rw_binds: tuple[BindSpec, ...] = Field(
        default=(),
        description=(
            "Host-пути read-write, формат `host[:target]`. Рендерятся "
            "{user_id}/{thread_id}; host-путь создаётся при вызове."
        ),
    )
    tmpfs: tuple[TmpfsSpec, ...] = Field(
        default=(),
        description=(
            "Mountpoints под tmpfs (in-memory), формат `dest[:size]`, "
            "например `/tmp:256M`."
        ),
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
        description=(
            "Рабочая директория внутри песочницы; поддерживает "
            "{user_id}/{thread_id}. Пустая = '/'."
        ),
    )

    @field_validator("rootfs", mode="after")
    @classmethod
    def _canonicalize_rootfs(cls, value: str) -> str:
        if not value:
            return value
        return os.path.normpath(os.path.abspath(os.path.expanduser(value)))

    @field_validator("ro_binds", "rw_binds", mode="before")
    @classmethod
    def _parse_binds(cls, value: object) -> object:
        if not isinstance(value, (list, tuple)):
            return value
        return tuple(
            BindSpec.parse(item) if isinstance(item, str) else item for item in value
        )

    @field_validator("tmpfs", mode="before")
    @classmethod
    def _parse_tmpfs(cls, value: object) -> object:
        if not isinstance(value, (list, tuple)):
            return value
        return tuple(
            TmpfsSpec.parse(item) if isinstance(item, str) else item for item in value
        )

    @field_validator("cwd", mode="after")
    @classmethod
    def _validate_cwd(cls, value: str) -> str:
        return BindSpec.check_vars(value)

    def render(self, variables: Mapping[str, str]) -> Self:
        """Профиль с подставленными значениями {user_id}/{thread_id}."""
        cwd = BindSpec.substitute(self.cwd, variables) if self.cwd else ""
        return self.model_copy(
            update={
                "ro_binds": tuple(b.render(variables) for b in self.ro_binds),
                "rw_binds": tuple(b.render(variables) for b in self.rw_binds),
                "cwd": cwd,
            }
        )
