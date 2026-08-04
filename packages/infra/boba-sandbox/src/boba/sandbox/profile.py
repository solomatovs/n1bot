"""Модели окружения запуска: профиль, реестр профилей, привязка к инструменту."""

from __future__ import annotations

import os
import string
from collections.abc import Mapping
from typing import Any, ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from boba.workspace.launcher import LauncherConfig

__all__ = [
    "BindSpec",
    "SandboxConfig",
    "SandboxProfile",
    "SandboxToolConfig",
    "TmpfsSpec",
]


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
            msg = f"bind {raw!r}: empty host path"
            raise ValueError(msg)
        host = cls._canonical(host)
        return cls(host=host, target=target if sep else host)

    @field_validator("host", "target", mode="after")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        cls.check_vars(value)
        if not value.startswith("/"):
            msg = f"bind: path must be absolute, got {value!r}"
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
            parsed = list(string.Formatter().parse(template))
        except ValueError as e:
            msg = f"invalid path template {template!r}: {e}"
            raise ValueError(msg) from e
        fields: list[str] = []
        for _, name, _, _ in parsed:
            if name is not None:
                fields.append(name)
        unknown = sorted(set(fields) - set(BindSpec.VARS))
        if unknown:
            msg = (
                f"unknown variables {unknown} in path {template!r} "
                f"(known: {', '.join(BindSpec.VARS)})"
            )
            raise ValueError(msg)
        return template

    @staticmethod
    def substitute(template: str, variables: Mapping[str, str]) -> str:
        try:
            return template.format_map(dict(variables))
        except KeyError as e:
            msg = (
                f"sandbox: variable {{{e.args[0]}}} in path {template!r} "
                f"is unavailable: no chainlit session"
            )
            raise RuntimeError(msg) from e

    @staticmethod
    def _canonical(path: str) -> str:
        return os.path.normpath(os.path.abspath(os.path.expanduser(path)))


class TmpfsSpec(BaseModel):
    """Один tmpfs `dest:size`; размер с суффиксом K/M/G либо в байтах."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(description="Mountpoint внутри песочницы.")
    size_bytes: int = Field(gt=0, description="Размер tmpfs в байтах; обязателен.")

    @classmethod
    def parse(cls, raw: str) -> Self:
        path, sep, size = raw.partition(":")
        if not sep:
            msg = f"tmpfs {raw!r}: size is required, use `dest:size` (256M, 1G)"
            raise ValueError(msg)
        return cls(path=path, size_bytes=cls._parse_size(size))

    @field_validator("path", mode="after")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        if not value.startswith("/"):
            msg = f"tmpfs: path must be absolute, got {value!r}"
            raise ValueError(msg)
        return os.path.normpath(value)

    @staticmethod
    def _parse_size(raw: str) -> int:
        factors = {"K": 1024, "M": 1024**2, "G": 1024**3}
        text = raw.strip().upper()
        factor = factors.get(text[-1:], 0)
        digits = text[:-1] if factor else text
        if not digits.isdigit() or int(digits) == 0:
            msg = f"tmpfs: invalid size {raw!r} (example: 256M, 1G)"
            raise ValueError(msg)
        return int(digits) * (factor or 1)


class SandboxProfile(BaseModel):
    """Параметры одной песочницы; LLM выбирает профиль по имени."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rootfs: str = Field(
        description=(
            "Каталог, монтируемый read-only как / песочницы; ro_binds "
            "ложатся поверх. Пустая строка — корень не монтируется. "
            "Собирается целью make sandbox-rootfs."
        ),
    )
    ro_binds: tuple[BindSpec, ...] = Field(
        description=(
            "Host-пути read-only, формат `host[:target]`; несуществующие "
            "пропускаются. Рендерятся {user_id}/{thread_id}."
        ),
    )
    rw_binds: tuple[BindSpec, ...] = Field(
        description=(
            "Host-пути read-write, формат `host[:target]`. Рендерятся "
            "{user_id}/{thread_id}; host-путь создаётся при вызове."
        ),
    )
    rw_images: tuple[BindSpec, ...] = Field(
        description=(
            "Ext4-образы read-write, формат `image[:target]`; монтируются "
            "через fuse2fs на время вызова. Рендерятся {user_id}/{thread_id}."
        ),
    )
    image_template: str = Field(
        description=(
            "Шаблонный ext4-образ; копируется в путь из rw_images при "
            "первом вызове. Обязателен, если rw_images непуст."
        ),
    )
    launcher: LauncherConfig = Field(
        description="Тайминги и размеры операций лаунчера образов (rw_images).",
    )
    tmpfs: tuple[TmpfsSpec, ...] = Field(
        description=(
            "Mountpoints под tmpfs (in-memory), формат `dest:size`, "
            "например `/tmp:256M`; размер обязателен."
        ),
    )
    network: bool = Field(
        description="False — `--unshare-net` (нет сети). True — сеть хоста.",
    )
    env_set: dict[str, str] = Field(
        description=(
            "Env внутри песочницы; host-env не наследуется. Для запуска "
            "утилит обычно нужен 'PATH'."
        ),
    )
    timeout_sec: int = Field(
        ge=1,
        le=3600,
        description="Жёсткий таймаут выполнения процесса (1..3600 сек).",
    )
    max_memory_bytes: int = Field(
        gt=0,
        description="Лимит памяти команды (RLIMIT_AS), байт; обязателен.",
    )
    max_cpu_sec: int = Field(
        gt=0,
        description="Лимит CPU-времени команды (RLIMIT_CPU), сек; обязателен.",
    )
    max_file_size_bytes: int = Field(
        gt=0,
        description=(
            "Лимит размера создаваемого файла (RLIMIT_FSIZE), байт; "
            "обязателен. Суммарный диск в rw_images ограничен самим образом."
        ),
    )
    max_open_files: int = Field(
        gt=0,
        description="Лимит открытых дескрипторов (RLIMIT_NOFILE); обязателен.",
    )
    max_processes: int = Field(
        gt=0,
        description=(
            "Лимит процессов на вызов (RLIMIT_NPROC); обязателен. "
            "Ставится через `ulimit -u` внутри песочницы: выставленный "
            "снаружи, он не даёт bwrap создать namespace."
        ),
    )
    max_output_bytes: int = Field(
        ge=1024,
        description="Лимит stdout И stderr по отдельности; сверх — обрезка.",
    )
    cgroup_base: str = Field(
        description=(
            "Делегированный cgroup v2 каталог; в нём создаётся leaf на "
            "каждый запуск. Обязателен, если задан любой cgroup_*-лимит."
        ),
    )
    cgroup_memory_bytes: int | None = Field(
        default=None,
        gt=0,
        description=(
            "Лимит памяти всего запуска суммарно (cgroup memory.max), байт: "
            "в отличие от max_memory_bytes не множится на число процессов. "
            "Каждый cgroup_*-параметр независим: отсутствие в конфиге — "
            "не контролируется; заданный обязан примениться, иначе "
            "ошибка старта."
        ),
    )
    cgroup_cpu_percent: int | None = Field(
        default=None,
        gt=0,
        description=(
            "Потолок скорости CPU всего запуска (cgroup cpu.max) в процентах "
            "одного ядра: 100 — ядро, 150 — полтора. Отсутствие — "
            "не контролируется."
        ),
    )
    cgroup_cpu_weight: int | None = Field(
        default=None,
        ge=1,
        le=10000,
        description=(
            "Вес CPU при конкуренции (cgroup cpu.weight), 1..10000; на "
            "простаивающей машине не ограничивает. Отсутствие — "
            "не контролируется."
        ),
    )
    cgroup_pids_max: int | None = Field(
        default=None,
        gt=0,
        description=(
            "Потолок числа процессов в группе (cgroup pids.max): в отличие "
            "от max_processes (RLIMIT_NPROC, общий на uid) считает только "
            "процессы этого запуска. Отсутствие — не контролируется."
        ),
    )
    cgroup_swap_max_bytes: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Лимит свопа группы (cgroup memory.swap.max), байт; 0 — своп "
            "запрещён, без него cgroup_memory_bytes дыряв: группа вытекает "
            "в swap. Отсутствие — не контролируется."
        ),
    )
    cgroup_oom_kill_all: bool | None = Field(
        default=None,
        description=(
            "cgroup memory.oom.group: при OOM убивать всю группу разом, "
            "а не один процесс — полуживой запуск бесполезен. "
            "Отсутствие — поведение ядра по умолчанию (один процесс)."
        ),
    )
    oom_score_adj: int = Field(
        ge=0,
        le=1000,
        description=(
            "oom_score_adj команды (0..1000): чем выше, тем охотнее OOM "
            "killer убьёт её, а не сервер. 0 — не менять."
        ),
    )
    cwd: str = Field(
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

    @field_validator("image_template", mode="after")
    @classmethod
    def _canonicalize_template(cls, value: str) -> str:
        if not value:
            return value
        return os.path.normpath(os.path.abspath(os.path.expanduser(value)))

    @model_validator(mode="after")
    def _validate_images(self) -> Self:
        if self.rw_images and not self.image_template:
            msg = "sandbox: rw_images is set, but image_template is empty"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _validate_cgroup(self) -> Self:
        group_fields = (
            self.cgroup_memory_bytes,
            self.cgroup_cpu_percent,
            self.cgroup_cpu_weight,
            self.cgroup_pids_max,
            self.cgroup_swap_max_bytes,
            self.cgroup_oom_kill_all,
        )
        requested = any(value is not None for value in group_fields)
        if requested and not self.cgroup_base:
            msg = "sandbox: group limits are set, but cgroup_base is empty"
            raise ValueError(msg)
        if self.cgroup_base and not self.cgroup_base.startswith("/"):
            msg = f"sandbox: cgroup_base must be absolute, got {self.cgroup_base!r}"
            raise ValueError(msg)
        return self

    @field_validator("ro_binds", "rw_binds", "rw_images", mode="before")
    @classmethod
    def _parse_binds(cls, value: object) -> object:
        if not isinstance(value, (list, tuple)):
            return value
        parsed: list[object] = []
        for item in value:
            if isinstance(item, str):
                parsed.append(BindSpec.parse(item))
            else:
                parsed.append(item)
        return tuple(parsed)

    @field_validator("tmpfs", mode="before")
    @classmethod
    def _parse_tmpfs(cls, value: object) -> object:
        if not isinstance(value, (list, tuple)):
            return value
        parsed: list[object] = []
        for item in value:
            if isinstance(item, str):
                parsed.append(TmpfsSpec.parse(item))
            else:
                parsed.append(item)
        return tuple(parsed)

    @field_validator("cwd", mode="after")
    @classmethod
    def _validate_cwd(cls, value: str) -> str:
        return BindSpec.check_vars(value)

    def render(self, variables: Mapping[str, str]) -> Self:
        """Профиль с подставленными значениями {user_id}/{thread_id}."""
        cwd = ""
        if self.cwd:
            cwd = BindSpec.substitute(self.cwd, variables)
        ro_binds = self._render_binds(self.ro_binds, variables)
        rw_binds = self._render_binds(self.rw_binds, variables)
        rw_images = self._render_binds(self.rw_images, variables)
        return self.model_copy(
            update={
                "ro_binds": ro_binds,
                "rw_binds": rw_binds,
                "rw_images": rw_images,
                "cwd": cwd,
            }
        )

    @staticmethod
    def _render_binds(
        specs: tuple[BindSpec, ...],
        variables: Mapping[str, str],
    ) -> tuple[BindSpec, ...]:
        rendered: list[BindSpec] = []
        for spec in specs:
            rendered.append(spec.render(variables))
        return tuple(rendered)


class SandboxConfig(BaseModel):
    """Секция [sandbox]: профили, на которые ссылаются инструменты."""

    model_config = ConfigDict(extra="ignore")

    profiles: dict[str, SandboxProfile] = Field(
        min_length=1,
        description="Профили по имени; инструмент берёт нужный ссылкой.",
    )


class SandboxToolConfig(BaseModel):
    """Секция [tool.<name>.sandbox]: в каком окружении запускать инструмент."""

    model_config = ConfigDict(extra="ignore")

    profile: SandboxProfile = Field(
        description='Профиль ссылкой: profile = "${sandbox.profiles.<name>}".',
    )
    override: Mapping[str, Any] = Field(
        description=(
            "Поля профиля, заменяемые для этого инструмента; пустая таблица "
            "означает «без изменений». Названное поле заменяет базовое целиком."
        ),
    )

    def effective(self) -> SandboxProfile:
        """Профиль запуска: база плюс то, что переопределил администратор."""
        if not self.override:
            return self.profile
        merged = self.profile.model_dump()
        merged.update(self.override)
        return SandboxProfile.model_validate(merged)
