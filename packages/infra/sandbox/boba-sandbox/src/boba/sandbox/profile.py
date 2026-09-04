"""Модели окружения запуска: профиль, реестр профилей, привязка к инструменту."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from boba.toolkit.template import FieldTemplate, TemplateError
from boba.workspace.binaries import SandboxBinary, TrustedBinaries
from boba.workspace.launcher import MountingConfig

__all__ = [
    "BindSpec",
    "IsolationSpec",
    "LimitsSpec",
    "MountsSpec",
    "RunSpec",
    "SandboxConfig",
    "SandboxHost",
    "SandboxLayout",
    "SandboxMount",
    "SandboxProfile",
    "SandboxToolConfig",
    "SetupBinds",
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
            FieldTemplate.parse(template).only(BindSpec.VARS)
        except TemplateError as e:
            msg = f"invalid path template {template!r}: {e}"
            raise ValueError(msg) from e

        return template

    @staticmethod
    def substitute(template: str, variables: Mapping[str, str]) -> str:
        """Подстановка значений вызова; чего нет — то и названо в отказе.

        Пустой набор значит вызов вне сессии, неполный — сессия есть, но
        чего-то в ней не хватает: например, вход не сохранён в базе, и у
        пользователя нет id.
        """
        try:
            return template.format_map(dict(variables))
        except KeyError as e:
            missing = e.args[0]

            if not variables:
                msg = (
                    f"sandbox: variable {{{missing}}} in path {template!r} "
                    f"is unavailable: the call has no user session"
                )
                raise RuntimeError(msg) from e

            known = ", ".join(sorted(variables))
            msg = (
                f"sandbox: variable {{{missing}}} in path {template!r} "
                f"is unavailable in this session (known: {known})"
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
        return cls(path=path, size_bytes=cls.parse_size(size))

    @field_validator("path", mode="after")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        if not value.startswith("/"):
            msg = f"tmpfs: path must be absolute, got {value!r}"
            raise ValueError(msg)
        return os.path.normpath(value)

    @staticmethod
    def parse_size(raw: str) -> int:
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

    template — эталон, с которого снимается копия при первом обращении
    пользователя. mount — сама копия и её точка внутри песочницы одной
    записью `host:target`, где host обязан содержать {user_id}: иначе все
    пользователи писали бы в один образ.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    USER_VAR: ClassVar[str] = "{user_id}"
    """Что делает образ персональным; без него копия была бы общей."""

    THREAD_VAR: ClassVar[str] = "{thread_id}"
    """Образ один на пользователя: вложения видны во всех его чатах."""

    template: str = Field(
        min_length=1,
        description=(
            "Шаблонный ext4-образ: с него копируется образ пользователя при "
            "первом обращении. Собирается целью make sandbox."
        ),
    )
    mount: BindSpec = Field(
        description=(
            "Образ пользователя и его точка монтирования, формат "
            "`host:target`, например "
            "`/var/lib/boba/workspace/{user_id}.ext4:/workspace`."
        ),
    )

    @field_validator("template", mode="after")
    @classmethod
    def _canonical(cls, value: str) -> str:
        """bwrap не примет относительный путь: корень песочницы read-only."""
        return os.path.normpath(os.path.abspath(os.path.expanduser(value)))

    @field_validator("mount", mode="before")
    @classmethod
    def _parse_mount(cls, value: object) -> object:
        if isinstance(value, str):
            return BindSpec.parse(value)

        return value

    @model_validator(mode="after")
    def _image_is_personal(self) -> Self:
        if self.THREAD_VAR in self.mount.host:
            msg = (
                f"sandbox: workspace.mount {self.mount.host!r} has "
                f"{self.THREAD_VAR}: the image is one per user, not per thread"
            )
            raise ValueError(msg)

        if self.USER_VAR not in self.mount.host:
            msg = (
                f"sandbox: workspace.mount {self.mount.host!r} has no "
                f"{self.USER_VAR}: the image would be shared by all users"
            )
            raise ValueError(msg)

        return self

    def images_dir(self) -> str:
        """Каталог, где лежат образы пользователей."""
        return os.path.dirname(self.mount.host)

    def image_of(self, user_id: str) -> str:
        """Путь образа пользователя на хосте."""
        if not user_id:
            msg = "workspace image requires a user id"
            raise ValueError(msg)

        return BindSpec.substitute(self.mount.host, {"user_id": user_id})


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


class SandboxMount(StrEnum):
    """Точки, которые песочница монтирует сама; конфиг их не задаёт.

    PROC и DEV обязательны: без них не работают python, bash и всё, что
    читает /proc/self. TMP приватен вызову — исполнитель перемонтирует его
    себе, размер приходит из профиля. SETUP_* видит только исполнитель,
    монтирующий образ workspace: перед запуском тела он их отцепляет, иначе
    тело видело бы каталог образов всех пользователей.
    """

    ROOTFS = "/tmp/boba-rootfs"  # noqa: S108  # nosec B108
    PROC = "/proc"
    DEV = "/dev"
    TMP = "/tmp"  # noqa: S108 — путь внутри песочницы  # nosec B108
    SETUP = "/mnt"
    SETUP_TEMPLATE = "/mnt/template.ext4"
    SETUP_FUSE2FS = "/mnt/fuse2fs"
    SETUP_IMAGES = "/mnt/images"

    def under(self, name: str) -> str:
        """Путь внутри этой точки."""
        return os.path.join(self.value, name)


class SetupBinds(BaseModel):
    """Обвязка монтирования образа: что исполнитель заносит и потом отцепляет."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ro: tuple[BindSpec, ...]
    rw: tuple[BindSpec, ...]


class SandboxLayout:
    """Монтирования, предопределённые песочницей.

    Профиль задаёт размер tmp и запись workspace; сами точки, обвязка
    образа, tmpfs под неё и путь образа внутри песочницы живут здесь и
    больше нигде.
    """

    DIR_BYTES: ClassVar[int] = 1024 * 1024
    """Размер tmpfs под обвязку: в ней лежат только точки монтирования."""

    @classmethod
    def tmpfs(cls, tmp_bytes: int, staged: bool) -> tuple[TmpfsSpec, ...]:
        """Все tmpfs секции: приватный /tmp и обвязка образа при workspace."""
        specs: list[TmpfsSpec] = [
            TmpfsSpec(path=SandboxMount.TMP.value, size_bytes=tmp_bytes)
        ]

        if staged:
            specs.append(
                TmpfsSpec(path=SandboxMount.SETUP.value, size_bytes=cls.DIR_BYTES)
            )

        return tuple(specs)

    @classmethod
    def setup_binds(cls, workspace: WorkspaceSpec, fuse2fs: str) -> SetupBinds:
        """Эталон и fuse2fs read-only, каталог образов пользователей на запись."""
        ro = (
            BindSpec(host=workspace.template, target=SandboxMount.SETUP_TEMPLATE.value),
            BindSpec(host=fuse2fs, target=SandboxMount.SETUP_FUSE2FS.value),
        )
        rw = (
            BindSpec(
                host=workspace.images_dir(), target=SandboxMount.SETUP_IMAGES.value
            ),
        )

        return SetupBinds(ro=ro, rw=rw)

    @classmethod
    def image_inside(cls, image_host: str) -> str:
        """Путь образа пользователя внутри песочницы: каталог образов фиксирован."""
        return SandboxMount.SETUP_IMAGES.under(os.path.basename(image_host))


class MountsSpec(BaseModel):
    """Что песочница монтирует и что из этого доезжает до тела инструмента.

    ro и rw видны на всех уровнях, включая тело. Остальное — proc, dev,
    приватный tmp и обвязку образа — песочница ставит сама: см.
    SandboxMount и SandboxLayout.
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
    tmp: int = Field(
        gt=0,
        description=(
            "Размер /tmp, например `512M`: он приватен вызову и служит "
            "единственным местом записи для тела без workspace."
        ),
    )
    workspace: WorkspaceSpec | None = Field(
        default=None,
        description=(
            "Рабочий каталог чата: эталон и образ пользователя с точкой "
            "монтирования. Отсутствие — у секции нет своего workspace."
        ),
    )

    @field_validator("ro", "rw", mode="before")
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

    @field_validator("tmp", mode="before")
    @classmethod
    def _parse_size(cls, value: object) -> object:
        if isinstance(value, str):
            return TmpfsSpec.parse_size(value)

        return value

    def all_binds(self) -> tuple[BindSpec, ...]:
        """Монтирования профиля, видимые телу; обвязку ставит SandboxLayout."""
        return (*self.ro, *self.rw)


class IsolationSpec(BaseModel):
    """Изоляция секции: сеть и окружение."""

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
            "Потолок числа процессов в группе (cgroup pids.max): считает "
            "процессы этого вызова."
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
    rootfs: str = Field(
        min_length=1,
        description=(
            "Ext4-образ корня, монтируемый read-only на всю жизнь зиготы; "
            "внутри уже лежат python, site-packages и код инструментов. "
            "Собирается целью make sandbox."
        ),
    )
    mounts: MountsSpec = Field(description="Что монтируется и что видит тело.")
    isolation: IsolationSpec = Field(description="Сеть и окружение.")
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

    def render(self, variables: Mapping[str, str]) -> Self:
        """Профиль с подставленными значениями {user_id}/{thread_id}."""
        update: dict[str, Any] = {
            "ro": self._render_binds(self.mounts.ro, variables),
            "rw": self._render_binds(self.mounts.rw, variables),
        }

        if workspace := self.mounts.workspace:
            rendered = workspace.mount.render(variables)
            update["workspace"] = workspace.model_copy(update={"mount": rendered})

        mounts = self.mounts.model_copy(update=update)

        cwd = ""
        if self.run.cwd:
            cwd = BindSpec.substitute(self.run.cwd, variables)

        run = self.run.model_copy(update={"cwd": cwd})

        return self.model_copy(update={"mounts": mounts, "run": run})

    def setup_binds(self) -> SetupBinds:
        """Обвязка монтирования образа; пустая у секции без workspace."""
        workspace = self.mounts.workspace
        if workspace is None:
            return SetupBinds(ro=(), rw=())

        fuse2fs = self.host.binaries.resolve(SandboxBinary.FUSE2FS)

        return SandboxLayout.setup_binds(workspace, fuse2fs)

    def tmpfs(self) -> tuple[TmpfsSpec, ...]:
        """Все tmpfs секции: приватный /tmp и обвязка образа при workspace."""
        staged = self.mounts.workspace is not None

        return SandboxLayout.tmpfs(self.mounts.tmp, staged)

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
