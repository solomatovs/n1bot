"""Модели окружения запуска: профиль, реестр профилей, привязка к инструменту."""

from __future__ import annotations

import math
import os
import string
from collections.abc import Mapping
from typing import Any, ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from boba.toolkit.binaries import TrustedBinaries
from boba.workspace.launcher import MountingConfig

__all__ = [
    "BindSpec",
    "IsolationSpec",
    "LimitsSpec",
    "MountsSpec",
    "RootfsSpec",
    "RunSpec",
    "SandboxConfig",
    "SandboxHost",
    "SandboxProfile",
    "SandboxToolConfig",
    "TmpfsSpec",
    "WorkspaceSpec",
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


class WorkspaceSpec(BaseModel):
    """Рабочий каталог чата: единственное описание workspace на всё приложение.

    Из него выводится всё остальное: путь образа пользователя собирается из
    каталога образов и его идентификатора, первый образ копируется с шаблона,
    внутрь песочницы он монтируется в одну и ту же точку. Отдельных настроек
    под каждый из этих путей нет — ошибиться и развести их невозможно.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    SUFFIX: ClassVar[str] = ".ext4"
    """Расширение образа пользователя; формат задаёт mkfs, а не администратор."""

    template: str = Field(
        min_length=1,
        description=(
            "Шаблонный ext4-образ: с него копируется образ пользователя при "
            "первом обращении. Собирается целью make sandbox-image."
        ),
    )
    images: str = Field(
        min_length=1,
        description=(
            "Каталог, где лежат образы пользователей. Имя файла компонент "
            "собирает сам из идентификатора пользователя."
        ),
    )
    mount: str = Field(
        min_length=1,
        description=(
            "Точка, куда образ пользователя монтируется внутри песочницы. "
            "По ней же приложение строит пути вложений, видимые инструменту."
        ),
    )

    @field_validator("template", "images", "mount", mode="after")
    @classmethod
    def _canonical(cls, value: str) -> str:
        """bwrap не примет относительный путь: корень песочницы read-only."""
        return os.path.normpath(os.path.abspath(os.path.expanduser(value)))

    def image_of(self, user_id: str) -> str:
        """Путь образа пользователя на хосте."""
        if not user_id:
            msg = "workspace image requires a user id"
            raise ValueError(msg)

        return os.path.join(self.images, f"{user_id}{self.SUFFIX}")


class SandboxHost(BaseModel):
    """Секция [sandbox.host]: то, что живёт в приложении и внутрь не попадает.

    Профиль ссылается на неё целиком; инструмент эти значения не меняет —
    они описывают не песочницу, а обвязку вокруг неё.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    binaries: TrustedBinaries = Field(
        description=(
            "Каталоги на хосте, откуда берутся bwrap и fuse2fs; $PATH не "
            "используется. Внутрь песочницы едет только путь fuse2fs, и то "
            "переведённый через бинды профиля."
        ),
    )
    mounting: MountingConfig = Field(
        description=(
            "Тайминги и размеры операций монтирования образов: корня при "
            "подъёме зиготы и образа пользователя на каждый вызов."
        ),
    )
    cgroup_base: str = Field(
        description=(
            "Делегированный cgroup v2 каталог на хосте: в нём приложение "
            "создаёт leaf на каждый вызов и вписывает туда исполнителя. "
            "Обязателен, если профиль задаёт хоть один group_*-лимит."
        ),
    )
    stderr_tail_bytes: int = Field(
        gt=0,
        description=(
            "Сколько последних байт stderr инструмента приложение держит в "
            "памяти, чтобы объяснить отказ, когда конверта с результатом нет."
        ),
    )
    channel_limit_bytes: int = Field(
        gt=0,
        description=(
            "Потолок на канал вызова, который приложение копит в своей памяти "
            "целиком: конверт результата и вывод shell-команды. Лимиты памяти "
            "песочницы на приложение не распространяются, поэтому без потолка "
            "тело инструмента выносит хост потоком в несколько гигабайт. "
            "Превышение обрывает вызов ошибкой инструмента."
        ),
    )
    fail_tail_chars: int = Field(
        gt=0,
        description=(
            "Сколько последних символов вывода попадает в строку журнала об "
            "отказе: длинный вывод в сообщении нечитаем."
        ),
    )
    kill_grace_sec: int = Field(
        gt=0,
        description=(
            "Сколько секунд приложение ждёт выхода процесса после kill, "
            "прежде чем считать его зависшим."
        ),
    )

    @field_validator("cgroup_base", mode="after")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not value:
            return value

        if not value.startswith("/"):
            msg = f"sandbox: cgroup_base must be absolute, got {value!r}"
            raise ValueError(msg)

        return value


class RootfsSpec(BaseModel):
    """Корень песочницы: либо образ, либо готовый каталог."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    image: str = Field(
        default="",
        description=(
            "Ext4-образ корня, монтируемый read-only на всю жизнь зиготы; "
            "внутри уже лежат python, site-packages и код инструментов. "
            "Собирается целью make sandbox-image. Взаимоисключим с dir."
        ),
    )
    dir: str = Field(
        default="",
        description=(
            "Готовый каталог, монтируемый read-only как / песочницы; бинды "
            "ложатся поверх. Пусто — корень берётся из image либо не "
            "монтируется вовсе."
        ),
    )
    mount: str = Field(
        default="",
        description=(
            "Куда цепочка запуска монтирует image перед тем, как сделать его "
            "корнем: путь виден только процессам цепочки, тело получает его "
            "уже как /. Обязателен, если задан image."
        ),
    )

    @field_validator("image", "dir", mode="after")
    @classmethod
    def _canonical(cls, value: str) -> str:
        if not value:
            return value

        return os.path.normpath(os.path.abspath(os.path.expanduser(value)))

    @model_validator(mode="after")
    def _one_root(self) -> Self:
        if self.dir and self.image:
            msg = "sandbox: rootfs.dir and rootfs.image are mutually exclusive"
            raise ValueError(msg)

        if self.image and not self.mount:
            msg = "sandbox: rootfs.image is set, but rootfs.mount is empty"
            raise ValueError(msg)

        return self


class MountsSpec(BaseModel):
    """Что песочница монтирует и что из этого доезжает до тела инструмента.

    ro и rw видны на всех уровнях, включая тело. setup_ro и setup_rw нужны
    исполнителю только для монтирования образа вызова: смонтировав свой
    образ, он отцепляет их от себя, и тело их не видит. Иначе тело видело бы
    каталог образов всех пользователей.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ro: tuple[BindSpec, ...] = Field(
        description=(
            "Host-пути read-only, формат `host[:target]`; несуществующие "
            "пропускаются. Рендерятся {user_id}/{thread_id}. Видны телу."
        ),
    )
    rw: tuple[BindSpec, ...] = Field(
        description=(
            "Host-пути read-write, формат `host[:target]`. Рендерятся "
            "{user_id}/{thread_id}. Видны телу."
        ),
    )
    setup_ro: tuple[BindSpec, ...] = Field(
        description=(
            "Read-only обвязка монтирования: шаблон образа и бинарь fuse2fs. "
            "Исполнитель отцепляет их перед запуском тела."
        ),
    )
    setup_rw: tuple[BindSpec, ...] = Field(
        description=(
            "Read-write обвязка монтирования: каталог образов пользователей. "
            "Исполнитель отцепляет её перед запуском тела."
        ),
    )
    tmpfs: tuple[TmpfsSpec, ...] = Field(
        description=(
            "Mountpoints под tmpfs (in-memory), формат `dest:size`, например "
            "`/tmp:256M`; размер обязателен."
        ),
    )
    call_tmpfs: str = Field(
        default="",
        description=(
            "Какая из точек tmpfs приватна для вызова: исполнитель "
            "перемонтирует её себе, чтобы файлы одного вызова не видел "
            "другой. Размер берётся из той же записи tmpfs. Пусто — вызовы "
            "делят её содержимое."
        ),
    )
    proc: str = Field(
        default="",
        description=(
            "Куда монтировать procfs. Пусто — /proc не монтируется: без него "
            "не работают python, bash и всё, что читает /proc/self."
        ),
    )
    dev: str = Field(
        default="",
        description=(
            "Куда монтировать минимальный devtmpfs (null, zero, random, tty). "
            "Пусто — устройств в песочнице нет."
        ),
    )
    workspace: WorkspaceSpec | None = Field(
        default=None,
        description=(
            "Рабочий каталог чата одной записью: шаблон, каталог образов и "
            "точка монтирования. Отсутствие — у секции нет своего workspace."
        ),
    )
    images: tuple[BindSpec, ...] = Field(
        description=(
            "Произвольные ext4-образы read-write, формат `image[:target]`; "
            "монтируются через fuse2fs на время вызова. Рендерятся "
            "{user_id}/{thread_id}."
        ),
    )
    image_template: str = Field(
        default="",
        description=(
            "Шаблонный ext4-образ для images: копируется в путь образа при "
            "первом вызове. Обязателен, если images непуст."
        ),
    )

    @field_validator("ro", "rw", "setup_ro", "setup_rw", "images", mode="before")
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

    @field_validator("image_template", mode="after")
    @classmethod
    def _canonical_template(cls, value: str) -> str:
        if not value:
            return value

        return os.path.normpath(os.path.abspath(os.path.expanduser(value)))

    @model_validator(mode="after")
    def _template_for_images(self) -> Self:
        if self.images and not self.image_template:
            msg = "sandbox: mounts.images is set, but mounts.image_template is empty"
            raise ValueError(msg)

        return self

    def all_binds(self) -> tuple[BindSpec, ...]:
        """Все монтирования профиля: и видимые телу, и обвязка."""
        return (*self.ro, *self.rw, *self.setup_ro, *self.setup_rw)


class IsolationSpec(BaseModel):
    """Изоляция секции: сеть, окружение, потолок задач."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    network: bool = Field(
        description="False — `--unshare-net` (нет сети). True — сеть хоста.",
    )
    env: dict[str, str] = Field(
        description=(
            "Env внутри песочницы; host-env не наследуется. Для запуска "
            "утилит обычно нужен 'PATH'."
        ),
    )
    max_processes: int | None = Field(
        default=None,
        gt=0,
        description=(
            "Общий потолок задач (RLIMIT_NPROC) на зиготу секции и всех её "
            "детей: счётчик один на user namespace, потоки считаются наравне "
            "с процессами. Ставится изнутри песочницы. Отсутствие — не "
            "ограничивать; потолок на отдельный вызов задаёт group_pids_max."
        ),
    )
    reap_poll_sec: float = Field(
        gt=0,
        description=(
            "Шаг опроса внутри зиготы: как часто её цикл просыпается сам, "
            "если не пришло ни сообщение от приложения, ни сигнал о смерти "
            "ребёнка. Смерть ребёнка будит цикл немедленно."
        ),
    )


class LimitsSpec(BaseModel):
    """Лимиты вызова: process_* — каждому процессу, group_* — всей группе.

    process_* ставит себе исполнитель через setrlimit, поэтому они действуют
    на каждый процесс вызова отдельно. group_* приложение выставляет
    cgroup-leaf'у и считает всю группу разом.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    timeout_sec: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Жёсткий таймаут вызова, сек: дедлайн считает приложение и "
            "убивает исполнителя. Отсутствие — не ограничивать."
        ),
    )
    process_memory_bytes: int = Field(
        gt=0,
        description="Лимит памяти процесса (RLIMIT_AS), байт; обязателен.",
    )
    process_cpu_sec: int = Field(
        gt=0,
        description="Лимит CPU-времени процесса (RLIMIT_CPU), сек; обязателен.",
    )
    process_file_bytes: int = Field(
        gt=0,
        description=(
            "Лимит размера создаваемого файла (RLIMIT_FSIZE), байт; "
            "обязателен. Суммарный диск образа ограничен самим образом."
        ),
    )
    process_open_files: int = Field(
        gt=0,
        description="Лимит открытых дескрипторов (RLIMIT_NOFILE); обязателен.",
    )
    process_oom_score_adj: int = Field(
        ge=0,
        le=1000,
        description=(
            "oom_score_adj процессов вызова (0..1000): чем выше, тем охотнее "
            "OOM killer убьёт их, а не сервер. 0 — не менять."
        ),
    )
    group_memory_bytes: int | None = Field(
        default=None,
        gt=0,
        description=(
            "Лимит памяти всей группы вызова (cgroup memory.max), байт: в "
            "отличие от process_memory_bytes не множится на число процессов. "
            "Отсутствие — не контролируется."
        ),
    )
    group_swap_bytes: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Лимит свопа группы (cgroup memory.swap.max), байт; 0 — своп "
            "запрещён, без него group_memory_bytes дыряв: группа вытекает в "
            "swap. Отсутствие — не контролируется."
        ),
    )
    group_cpu_percent: int | None = Field(
        default=None,
        gt=0,
        description=(
            "Потолок скорости CPU группы (cgroup cpu.max) в процентах одного "
            "ядра: 100 — ядро, 150 — полтора. По нему же считается маска "
            "ядер зиготы и исполнителя. Отсутствие — не контролируется."
        ),
    )
    group_cpu_weight: int | None = Field(
        default=None,
        ge=1,
        le=10000,
        description=(
            "Вес CPU при конкуренции (cgroup cpu.weight), 1..10000; на "
            "простаивающей машине не ограничивает."
        ),
    )
    group_pids_max: int | None = Field(
        default=None,
        gt=0,
        description=(
            "Потолок числа процессов в группе (cgroup pids.max): в отличие от "
            "max_processes считает только процессы этого вызова."
        ),
    )
    group_oom_kill_all: bool | None = Field(
        default=None,
        description=(
            "cgroup memory.oom.group: при OOM убивать всю группу разом, а не "
            "один процесс — полуживой вызов бесполезен."
        ),
    )

    def group_requested(self) -> bool:
        """Задан ли хоть один групповой лимит: иначе leaf не создаётся."""
        values = (
            self.group_memory_bytes,
            self.group_swap_bytes,
            self.group_cpu_percent,
            self.group_cpu_weight,
            self.group_pids_max,
            self.group_oom_kill_all,
        )
        for value in values:  # noqa: SIM110
            if value is not None:
                return True

        return False


class RunSpec(BaseModel):
    """Чем и где исполняется команда инструмента."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cwd: str = Field(
        description=(
            "Рабочая директория внутри песочницы; поддерживает "
            "{user_id}/{thread_id}. Пустая — корень."
        ),
    )
    shell: str = Field(
        default="",
        description=(
            "Интерпретатор, которым исполняется команда bash-инструмента. "
            "Обязателен для секций, где такой инструмент включён; путь "
            "должен существовать внутри корня песочницы."
        ),
    )

    @field_validator("cwd", mode="after")
    @classmethod
    def _validate_cwd(cls, value: str) -> str:
        return BindSpec.check_vars(value)


class SandboxProfile(BaseModel):
    """Песочница одной секции инструментов, собранная из пяти групп настроек.

    extends позволяет объявить профиль как правку другого: названные поля
    заменяют унаследованные, остальные приходят из базы. Инструмент выбирает
    готовый профиль ссылкой и ничего в нём не правит.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    GROUPS: ClassVar[tuple[str, ...]] = (
        "host",
        "rootfs",
        "mounts",
        "isolation",
        "limits",
        "run",
    )
    """Группы, которые сливаются при наследовании профиля."""

    host: SandboxHost = Field(
        description='Обвязка приложения ссылкой: host = "${sandbox.host}".',
    )
    rootfs: RootfsSpec = Field(description="Корень песочницы: образ либо каталог.")
    mounts: MountsSpec = Field(description="Что монтируется и что видит тело.")
    isolation: IsolationSpec = Field(description="Сеть, окружение, потолок задач.")
    limits: LimitsSpec = Field(description="Лимиты процесса и группы вызова.")
    run: RunSpec = Field(description="Рабочий каталог и интерпретатор команды.")

    MAX_EXTENDS: ClassVar[int] = 8
    """Потолок длины цепочки наследования: защита от ссылки профиля на себя."""

    @model_validator(mode="before")
    @classmethod
    def _inherit(cls, value: object) -> object:
        """extends — база профиля: названные группы правятся, прочие берутся из неё.

        База сама может быть наследником, поэтому цепочка разворачивается до
        профиля без extends.
        """
        if not isinstance(value, Mapping):
            return value

        raw = dict(value)
        for _ in range(cls.MAX_EXTENDS):
            base = raw.pop("extends", None)
            if base is None:
                return raw

            if not isinstance(base, Mapping):
                msg = "sandbox: extends must reference a profile table"
                raise ValueError(msg)

            raw = cls._merge(dict(base), raw)

        msg = f"sandbox: extends chain is longer than {cls.MAX_EXTENDS} profiles"
        raise ValueError(msg)

    @classmethod
    def _merge(cls, base: dict[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
        """Слияние на два уровня: группа -> поле; списки заменяются целиком."""
        merged = dict(base)
        for name, value in patch.items():
            if name not in cls.GROUPS:
                merged[name] = value
                continue

            group = merged.get(name)
            if not isinstance(group, Mapping) or not isinstance(value, Mapping):
                merged[name] = value
                continue

            updated = dict(group)
            updated.update(value)
            merged[name] = updated

        return merged

    @model_validator(mode="after")
    def _validate_groups(self) -> Self:
        if self.limits.group_requested() and not self.host.cgroup_base:
            msg = "sandbox: group limits are set, but host.cgroup_base is empty"
            raise ValueError(msg)

        return self

    @model_validator(mode="after")
    def _validate_setup_binds(self) -> Self:
        """Обвязка монтирования объявляется только там, где есть что монтировать.

        Она заносит внутрь каталог образов всех пользователей, поэтому
        профилю без образов её объявлять нечем и незачем.
        """
        setup = (*self.mounts.setup_ro, *self.mounts.setup_rw)
        if not setup:
            return self

        if self.mounts.images:
            return self

        if self.mounts.workspace is not None:
            return self

        listed = ", ".join(spec.target for spec in setup)
        msg = (
            "sandbox: setup_ro/setup_rw are declared, but the profile mounts "
            f"no images: {listed}"
        )
        raise ValueError(msg)

    def render(self, variables: Mapping[str, str]) -> Self:
        """Профиль с подставленными значениями {user_id}/{thread_id}."""
        mounts = self.mounts.model_copy(
            update={
                "ro": self._render_binds(self.mounts.ro, variables),
                "rw": self._render_binds(self.mounts.rw, variables),
                "setup_ro": self._render_binds(self.mounts.setup_ro, variables),
                "setup_rw": self._render_binds(self.mounts.setup_rw, variables),
                "images": self._render_binds(self.mounts.images, variables),
            }
        )

        cwd = ""
        if self.run.cwd:
            cwd = BindSpec.substitute(self.run.cwd, variables)

        run = self.run.model_copy(update={"cwd": cwd})

        return self.model_copy(update={"mounts": mounts, "run": run})

    def cpu_cores(self) -> int:
        """Сколько ядер отдано вызову по cgroup-квоте; 0 — квоты нет.

        Нативные движки (onnxruntime, BLAS) размер своего пула берут из маски
        доступных ядер, а cgroup-квоту не видят: без маски они поднимают пул
        по числу ядер машины и дерутся за выданную долю. Запуск переводит
        квоту в число ядер и ставит процессу affinity.
        """
        percent = self.limits.group_cpu_percent
        if percent is None:
            return 0

        return max(1, math.ceil(percent / 100))

    def inside(self, host: str) -> str:
        """Путь хоста внутри песочницы по биндам профиля.

        Берётся самый длинный покрывающий бинд; пустая строка — ни один бинд
        этот путь внутрь не приносит.
        """
        best = BindSpec(host="/", target="/")
        found = False
        for spec in self.mounts.all_binds():
            if not self._covers(spec.host, host):
                continue

            if found and len(spec.host) <= len(best.host):
                continue

            best = spec
            found = True

        if not found:
            return ""

        if host == best.host:
            return best.target

        return os.path.join(best.target, os.path.relpath(host, best.host))

    @staticmethod
    def _covers(bind_host: str, path: str) -> bool:
        if path == bind_host:
            return True

        return path.startswith(bind_host.rstrip("/") + "/")

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
