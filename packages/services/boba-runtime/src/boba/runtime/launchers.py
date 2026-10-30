"""Способ запуска плагинов: [tool_launcher] выбирает песочницу или процесс хоста.

Проверки старта и сборка исполнителей упакованы в реализации SectionLaunchers:
песочница проверяет cgroup-лимиты профилей и bwrap, процесс хоста — workdir.
Обе реализации зовут подкоманды программы плагина: manifest и setup на старте,
zygote или call на вызовах.

Ошибки:
RuntimeError — запуск не согласован с конфигом: нет [tool_launcher],
    [sandbox] или [tool.<id>.sandbox], bwrap недоступен, workdir отсутствует;
    программа плагина не отдала манифест или не подготовила хранилище.
ZygoteStartError — зигота плагина не поднялась за отведённые попытки (settle).
"""

from __future__ import annotations

import json
import subprocess
import sys
from abc import abstractmethod
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, Protocol

from omegaconf import DictConfig, OmegaConf
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    model_validator,
)

from boba.canvas.keys import WorkspaceMount
from boba.config import bind
from boba.identity.context import CallContexts
from boba.krb import KerberosWorkspace
from boba.runtime.config import (
    InstanceConfig,
    InstanceData,
    ProgramRoot,
    ProgramRoots,
)
from boba.sandbox import (
    BindSpec,
    CgroupProbe,
    SandboxProfile,
    WorkspaceSpec,
    has_bwrap,
)
from boba.sandbox.program import PluginCommand, PluginEntry
from boba.sandbox.runner import SandboxLogRelay
from boba.sandbox.zygote import (
    ZygotePolicy,
    ZygoteRegistry,
    ZygoteSection,
    ZygoteToolCaller,
)
from boba.toolkit.chain import CallAmbient
from boba.toolkit.launcher import ToolLauncher
from boba.toolkit.manifest import (
    ManifestError,
    ManifestReader,
    PluginManifest,
)
from boba.toolrun.process import ProcessLauncherConfig, ProcessToolCaller

__all__ = [
    "CallSurface",
    "PluginImage",
    "PluginLaunch",
    "PluginOutput",
    "PluginSandbox",
    "ProcessLaunchers",
    "SandboxDefaults",
    "SandboxLauncherConfig",
    "SectionLaunchers",
    "ToolLauncherConfig",
    "ToolLaunchers",
    "ZygoteLaunchers",
]


@dataclass(frozen=True)
class PluginLaunch:
    """Что способу запуска нужно от плагина: его манифест и секция [tool.<id>]
    значениями JSON. Собирает его загрузчик плагинов после чтения манифеста."""

    manifest: PluginManifest
    section: Mapping[str, Any]

    @property
    def id(self) -> str:
        return self.manifest.id


class PluginOutput:
    """Вывод подкоманд manifest и setup программы плагина: код возврата,
    stdout и stderr становятся манифестом либо отказом старта с текстом
    программы. Общий у песочницы и процесса хоста."""

    def __init__(self) -> None:
        self._reader = ManifestReader()

    def manifest(
        self, plugin_id: str, done: subprocess.CompletedProcess[bytes], where: str
    ) -> PluginManifest:
        self.finished(plugin_id, PluginCommand.MANIFEST, done, where)

        try:
            manifest = self._reader.read(done.stdout)
        except ManifestError as exc:
            msg = f"plugin {plugin_id}: `manifest` in {where} printed {exc}"
            raise RuntimeError(msg) from exc

        if manifest.id != plugin_id:
            msg = (
                f"plugin {plugin_id}: `manifest` in {where} describes plugin "
                f"{manifest.id!r}: the image and the config name different plugins"
            )
            raise RuntimeError(msg)

        return manifest

    def finished(
        self,
        plugin_id: str,
        command: PluginCommand,
        done: subprocess.CompletedProcess[bytes],
        where: str,
    ) -> None:
        """Подкоманда command кончилась кодом 0; иначе — отказ с её stderr."""
        if done.returncode == 0:
            return

        stderr = "\n".join(self._own_lines(done.stderr)).strip()
        msg = (
            f"plugin {plugin_id}: `{command}` in {where} exited with code "
            f"{done.returncode}: {stderr[-2000:]}"
        )
        raise RuntimeError(msg)

    @staticmethod
    def _own_lines(stderr: bytes) -> Iterator[str]:
        """Строки программы плагина: ход монтирования песочницы — не её текст."""
        for line in stderr.decode("utf-8", errors="replace").splitlines():
            if SandboxLogRelay.relayed(line):
                continue

            yield line

    def section_bytes(self, section: Mapping[str, Any]) -> bytes:
        """Секция на stdin подкоманды."""
        return json.dumps(dict(section), ensure_ascii=False).encode("utf-8")


class SandboxCgroupConfig(BaseModel):
    """Ключ [tool_launcher], на котором стоят профили песочницы: каталог cgroup
    песочниц экземпляра. Профиль плагина собирается и тогда, когда процесс
    выбрал другой способ (стенды тестов), поэтому ключ читается отдельно от
    provider."""

    model_config = ConfigDict(extra="ignore")

    cgroup_base: str = Field(
        min_length=1,
        description=(
            "Каталог cgroup песочниц экземпляра: подкаталог sandbox юнита "
            "boba-sandbox@<экземпляр>.service; его вписывает make."
        ),
    )


class SandboxLauncherConfig(SandboxCgroupConfig):
    """Секция [tool_launcher] provider = sandbox: запуск в песочнице bwrap.

    Лишние ключи игнорируются: секция общая для всех провайдеров, provider
    выбирается ссылкой из конфига экземпляра.
    """

    provider: Literal["sandbox"]


ToolLauncherConfig = Annotated[
    SandboxLauncherConfig | ProcessLauncherConfig,
    Field(discriminator="provider"),
]
"""Discriminated union по provider — точная диагностика ошибок валидации."""


class ToolLauncherSection(RootModel[ToolLauncherConfig]):
    """Секция [tool_launcher] целиком."""


class PluginImage(BaseModel):
    """Каталог данных плагина из `images` конфига: `путь:точка`. Путь —
    от каталога программы (ProgramRoot.located) либо абсолютный, точка —
    абсолютный путь внутри песочницы."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    target: str = Field(min_length=1, pattern=r"^/")

    @model_validator(mode="before")
    @classmethod
    def _parse(cls, value: object) -> object:
        if not isinstance(value, str):
            return value

        path, separator, target = value.partition(":")
        if not separator:
            msg = (
                f"[sandbox] images expects entries `path:target`, got {value!r} "
                "without the target inside the sandbox"
            )
            raise ValueError(msg)

        return {"path": path, "target": target}


class PluginSandbox(BaseModel):
    """Секция [sandbox] файла conf/plugins/<id>.toml: что и как запускать.

    program — каталог программы плагина (бинд в /plugin), rootfs — ext4-образ
    корня, images — каталоги данных `каталог:точка`. Пути относительные —
    от каталога программы сервиса (ProgramRoot.located), абсолютные — как
    есть. workspace — образ рабочего каталога пользователя. Сеть плагин
    объявляет манифестом. Таблицы host/isolation/limits/run/zygote
    накладываются на собранный дефолт полями профиля.
    """

    model_config = ConfigDict(extra="forbid")

    program: str = Field(min_length=1)
    rootfs: str = Field(min_length=1)
    images: tuple[PluginImage, ...] = ()
    workspace: bool = False
    host: dict[str, Any] = Field(default_factory=dict)
    isolation: dict[str, Any] = Field(default_factory=dict)
    limits: dict[str, Any] = Field(default_factory=dict)
    run: dict[str, Any] = Field(default_factory=dict)
    zygote: dict[str, Any] = Field(default_factory=dict)


class PluginLayout(StrEnum):
    """Программа плагина в песочнице: каталог программы биндится в DIR, её
    вход RUN принимает id плагина первым аргументом."""

    DIR = "/plugin"
    RUN = "/plugin/run"


class SandboxDefaults:
    """Дефолтный профиль песочницы: числа здесь, пути — от каталога программы
    и каталога данных экземпляра.

    Секции [sandbox] в корневом конфиге нет: всё выводимое строится кодом,
    файл плагина описывает только отличия, манифест — сеть и образы данных.
    Память группы вызова кодом не выводится: её объявляет [sandbox.limits]
    каждого плагина, а профиль без неё не собирается. Создаёт его способ
    запуска в песочнице (ZygoteLaunchers).
    """

    TMP_SIZE: ClassVar[str] = "512M"

    NETWORK_BINDS: ClassVar[tuple[str, ...]] = (
        "/etc/resolv.conf:/etc/resolv.conf",
        "/etc/hosts:/etc/hosts",
    )
    """Файлы хоста, без которых у плагина с сетью нет имён."""
    KRB5_CONF: ClassVar[str] = "/etc/krb5.conf"
    """Куда плагину с сетью кладётся krb5.conf процесса: нужен для билетов."""
    CA_FILE: ClassVar[str] = "/etc/ssl/certs/ca-certificates.crt"
    """Куда плагину с сетью кладётся CA-файл контура ([instance].ca_file)."""

    HOST: ClassVar[dict[str, Any]] = {
        "stderr_tail_bytes": 4096,
        "channel_limit_bytes": 67108864,
        "fail_tail_chars": 2000,
        "kill_grace_sec": 5,
        "mounting": {
            "mount_wait_sec": 10.0,
            "mount_poll_sec": 0.002,
            "shutdown_wait_sec": 5.0,
            "lock_wait_sec": 10.0,
            "copy_chunk_bytes": 1048576,
        },
    }

    ISOLATION: ClassVar[dict[str, Any]] = {
        "network": False,
        "reap_poll_sec": 0.1,
        "env": {
            "PATH": "/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin",
            "HOME": "/tmp",  # noqa: S108 — путь внутри песочницы  # nosec B108
            "LANG": "C.UTF-8",
        },
    }

    LIMITS: ClassVar[dict[str, Any]] = {
        "timeout_sec": 86400,
        "process_memory_bytes": 1073741824,
        "process_cpu_sec": 86400,
        "process_file_bytes": 1073741824,
        "process_open_files": 1024,
        "process_oom_score_adj": 900,
        "group_swap_bytes": 0,
        "group_cpu_percent": 100,
        "group_cpu_weight": 100,
        "group_pids_max": 256,
        "group_oom_kill_all": True,
    }

    def __init__(
        self, program: ProgramRoot, instance: InstanceConfig, cgroup_base: str
    ) -> None:
        self._program = program
        self._instance = instance
        self._cgroup_base = cgroup_base

    def network_binds(self) -> list[str]:
        """Имена, krb5.conf процесса (если kerberos настроен файлом) и CA
        контура."""
        binds = [*self.NETWORK_BINDS, f"{self._instance.ca_file}:{self.CA_FILE}"]

        krb5_conf = KerberosWorkspace.krb5_config()
        if Path(krb5_conf).is_file():
            binds.append(f"{krb5_conf}:{self.KRB5_CONF}")

        return binds

    def network_env(self) -> dict[str, str]:
        return {InstanceConfig.CA_ENV: self.CA_FILE}

    def images(self, sandbox: PluginSandbox) -> list[str]:
        """Каталог программы плагина в /plugin и каталоги данных конфига —
        пути от каталога программы сервиса."""
        program = self._program.located(sandbox.program)
        images = [f"{program}:{PluginLayout.DIR.value}"]
        for image in sandbox.images:
            images.append(f"{self._program.located(image.path)}:{image.target}")

        return images

    def profile(self, sandbox: PluginSandbox) -> dict[str, Any]:
        binaries = self._program.binaries()

        return {
            "rootfs": str(self._program.located(sandbox.rootfs)),
            "host": {
                "cgroup_base": self._cgroup_base,
                "binaries": {"dirs": list(binaries.dirs)},
                **self.HOST,
            },
            "mounts": {
                "ro": [],
                "rw": [],
                "images": self.images(sandbox),
                "tmp": self.TMP_SIZE,
            },
            "isolation": self._isolation(),
            "limits": dict(self.LIMITS),
            "run": {"cwd": "/tmp"},  # noqa: S108 — внутри песочницы  # nosec B108
        }

    def workspace(self) -> WorkspaceSpec:
        return self._program.workspace(self._instance.data(InstanceData.WORKSPACE))

    def _isolation(self) -> dict[str, Any]:
        isolation = dict(self.ISOLATION)
        isolation["env"] = dict(self.ISOLATION["env"])

        return isolation

    @staticmethod
    def merged(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
        """Рекурсивное наложение таблиц файла плагина на дефолтный профиль."""
        result = dict(base)
        for key, value in override.items():
            below = result.get(key)
            if isinstance(below, dict) and isinstance(value, Mapping):
                result[key] = SandboxDefaults.merged(below, value)
            else:
                result[key] = value

        return result


class CallSurface:
    """Значения из контекста вызова для исполнителя песочницы.

    Создаёт его запуск секции из держателя контекста процесса; метод
    отдаётся исполнителю песочницы источником переменных путей профиля.
    """

    def __init__(self, contexts: CallContexts) -> None:
        self._contexts = contexts

    def sandbox_path_vars(self) -> dict[str, str]:
        """Значения {user_id}/{scope_id} для путей профиля на момент вызова.

        Вне контекста вызова значений нет: профиль с такими переменными
        отказывает рендером, называя недостающую.
        """
        context = self._contexts.peek()
        if context is None:
            return {}

        return {
            BindSpec.VARS[0]: context.subject.user_key,
            BindSpec.VARS[1]: context.scope.id,
        }


class SectionLaunchers(Protocol):
    """Способ запуска плагинов: проверки старта, подкоманды программы
    плагина и исполнители его вызовов."""

    @abstractmethod
    def probe(self) -> None:
        """Проверяет предпосылки способа запуска; нарушение — отказ старта."""

    @abstractmethod
    def manifest_of(self, plugin_id: str, section: Mapping[str, Any]) -> PluginManifest:
        """Манифест плагина от его программы: подкоманда manifest с секцией
        section на stdin."""

    @abstractmethod
    def setup(self, launch: PluginLaunch, section: Mapping[str, Any]) -> None:
        """Хранилище плагина: подкоманда setup с секцией section, у которой
        keytab-профили уже заменены билетами."""

    @abstractmethod
    def launcher_of(
        self, launch: PluginLaunch, contexts: CallContexts, ambient: CallAmbient
    ) -> ToolLauncher:
        """Исполнитель вызовов плагина; его изоляция проверяется здесь.
        contexts — держатель контекста вызова: по нему исполнитель узнаёт
        пользователя и область на момент вызова."""

    @abstractmethod
    def settle(self) -> None:
        """Дожидается подъёма исполнителей, начатого launcher_of: сборка
        инструментов зовёт его после всех плагинов, неудача — отказ старта."""

    @abstractmethod
    def stop(self) -> None:
        """Гасит всё, что способ запуска держит между вызовами; зовётся на
        остановке процесса."""


class NoLaunchers(SectionLaunchers):
    """Реализация SectionLaunchers процесса, который плагинов инструментов не
    исполняет: в его конфиге нет секции [tool_launcher]. Так собран чат —
    его инструменты исполняет сервис boba-mcp. Плагин в конфиге такого
    процесса получает отказ на сборке реестра."""

    def probe(self) -> None:
        return

    def manifest_of(self, plugin_id: str, section: Mapping[str, Any]) -> PluginManifest:
        raise RuntimeError(self._refusal(plugin_id))

    def setup(self, launch: PluginLaunch, section: Mapping[str, Any]) -> None:
        raise RuntimeError(self._refusal(launch.id))

    def launcher_of(
        self, launch: PluginLaunch, contexts: CallContexts, ambient: CallAmbient
    ) -> ToolLauncher:
        raise RuntimeError(self._refusal(launch.id))

    def settle(self) -> None:
        return

    def stop(self) -> None:
        return

    @staticmethod
    def _refusal(plugin_id: str) -> str:
        return (
            f"tool plugin {plugin_id!r} cannot be launched: the config has no "
            "[tool_launcher] section, expected provider = sandbox | process"
        )


class ZygoteLaunchers(SectionLaunchers):
    """Запуск в песочнице: зигота на плагин, профиль собирается из каталога
    программы, каталога данных экземпляра, дефолтов, манифеста и секции
    [sandbox] файла плагина.

    Реализация SectionLaunchers; объект один на процесс, зиготы плагинов
    живут в его реестре (ZygoteRegistry) между сборками инструментов. Каталог
    программы (ProgramRoot) разрешает пути из конфига плагина и даёт bwrap и
    интерпретатор для цепочки лаунчера. Программа плагина — `/plugin/run <id>`
    его каталога: manifest идёт в песочнице без сети и workspace, setup и
    зигота — в профиле плагина."""

    def __init__(self, raw: DictConfig) -> None:
        self._raw = raw
        self._program = ProgramRoots().locate()
        section = bind(raw, ToolLaunchers.SECTION, SandboxCgroupConfig)
        instance = bind(raw, InstanceConfig.SECTION, InstanceConfig)
        self._defaults = SandboxDefaults(self._program, instance, section.cgroup_base)
        self._zygotes = ZygoteRegistry(self._program.interpreter)
        self._cgroups = CgroupProbe(self._program.interpreter.probe)
        self._output = PluginOutput()

    def settle(self) -> None:
        self._zygotes.settle()

    def stop(self) -> None:
        self._zygotes.stop_all()

    def probe(self) -> None:
        """Предпосылок у способа нет: пути программы, корня и данных называет
        конфиг каждого плагина, их проверяет запуск его подкоманды, а
        cgroup-лимиты — сборка профиля."""
        return

    def manifest_of(self, plugin_id: str, section: Mapping[str, Any]) -> PluginManifest:
        sandbox = self._plugin_sandbox(plugin_id)
        profile = self._checked(self._bare(sandbox), plugin_id)
        done = self._zygotes.run(
            profile,
            ZygotePolicy.model_validate(sandbox.zygote),
            (PluginLayout.RUN, plugin_id, PluginCommand.MANIFEST),
            self._output.section_bytes(section),
        )

        return self._output.manifest(plugin_id, done, profile.rootfs)

    def setup(self, launch: PluginLaunch, section: Mapping[str, Any]) -> None:
        sandbox = self._plugin_sandbox(launch.id)
        profile = self.profile_of(launch)
        done = self._zygotes.run(
            profile,
            ZygotePolicy.model_validate(sandbox.zygote),
            (PluginLayout.RUN, launch.id, PluginCommand.SETUP),
            self._output.section_bytes(section),
        )

        self._output.finished(launch.id, PluginCommand.SETUP, done, profile.rootfs)

    def launcher_of(
        self, launch: PluginLaunch, contexts: CallContexts, ambient: CallAmbient
    ) -> ToolLauncher:
        profile = self.profile_of(launch)
        sandbox = self._plugin_sandbox(launch.id)

        supervisor = self._zygotes.prepare(
            ZygoteSection(
                name=launch.id,
                profile=profile,
                program=(PluginLayout.RUN, launch.id, PluginCommand.ZYGOTE),
                policy=ZygotePolicy.model_validate(sandbox.zygote),
                warmup=launch.section,
                manifest=launch.manifest.digest(),
            )
        )

        surface = CallSurface(contexts)

        return ZygoteToolCaller(
            launch.id, supervisor, profile, ambient, surface.sandbox_path_vars
        )

    def _plugin_sandbox(self, plugin_id: str) -> PluginSandbox:
        return bind(self._raw, f"tool.{plugin_id}.sandbox", PluginSandbox)

    def profile_of(self, launch: PluginLaunch) -> SandboxProfile:
        """Профиль плагина по [sandbox] его файла: дефолты плюс корень,
        программа и каталоги данных из конфига, сеть манифеста и дельта
        изоляции.

        Профиль есть, а bwrap недоступен — отказ старта: молчаливая деградация
        инструмента до процесса приложения запрещена.
        """
        sandbox = self._plugin_sandbox(launch.id)
        data = self._composed(sandbox)
        if launch.manifest.network:
            data["isolation"]["network"] = True
            data["isolation"]["env"].update(self._defaults.network_env())
            data["mounts"]["ro"] = self._defaults.network_binds()

        if sandbox.workspace:
            data["mounts"]["workspace"] = self._defaults.workspace()
            data["run"]["cwd"] = ProgramRoot.WORKSPACE_TARGET

        return self._checked(SandboxProfile.model_validate(data), launch.id)

    def _bare(self, sandbox: PluginSandbox) -> SandboxProfile:
        """Профиль подкоманды manifest: без сети и workspace — манифест о сети
        ещё не сказал."""
        return SandboxProfile.model_validate(self._composed(sandbox))

    def _checked(self, profile: SandboxProfile, plugin_id: str) -> SandboxProfile:
        if profile.mounts.workspace is not None:
            WorkspaceMount.configure(profile.mounts.workspace.mount.target)

        if not has_bwrap(profile):
            msg = (
                f"sandbox profile of {plugin_id!r} is configured, but bubblewrap "
                "(bwrap) is not in the trusted binary directories"
            )
            raise RuntimeError(msg)

        # групповые лимиты плагина проверяются при сборке: отказ виден на старте
        self._cgroups.profiles({plugin_id: profile})
        return profile

    def _composed(self, needs: PluginSandbox) -> dict[str, Any]:
        """Дефолтный профиль программы + корень, программа и данные + таблицы
        из [sandbox] файла conf/plugins/<id>.toml."""
        data = self._defaults.profile(needs)

        overrides = {
            "host": needs.host,
            "isolation": needs.isolation,
            "limits": needs.limits,
            "run": needs.run,
        }
        for key, override in overrides.items():
            if override:
                data[key] = SandboxDefaults.merged(data[key], override)

        return data


class ProcessLaunchers(SectionLaunchers):
    """Запуск субпроцессом хоста: без песочницы, файлы инструментов в workdir —
    каталоге workspace данных экземпляра.

    Программа плагина — `python -m boba.sandbox.program <id>` интерпретатором
    приложения: плагин стоит в окружении исходников, режим — только отладка.
    Собранная программа (без python) этот способ отвергает на старте.
    """

    def __init__(self, cfg: ProcessLauncherConfig, workdir: Path) -> None:
        self._cfg = cfg
        self._workdir = workdir
        self._output = PluginOutput()
        self._entry = PluginEntry()
        self._programs = ProgramRoots()

    def probe(self) -> None:
        if self._programs.compiled():
            msg = (
                "[tool_launcher] provider = process runs tool plugins with the python "
                f"of the sources environment, but this program is compiled and has "
                f'no python ({sys.executable}); set provider = "sandbox" or run '
                "the service from the sources with BOBA_RUNTIME"
            )
            raise RuntimeError(msg)

        if not self._workdir.is_dir():
            msg = (
                "[tool_launcher] provider = process: the workspace directory of "
                f"the instance data must exist, {self._workdir} does not"
            )
            raise RuntimeError(msg)

        # файловые ссылки канваса читают файлы инструментов из workdir
        WorkspaceMount.configure(str(self._workdir))

    def manifest_of(self, plugin_id: str, section: Mapping[str, Any]) -> PluginManifest:
        done = self._run(plugin_id, PluginCommand.MANIFEST, section)

        return self._output.manifest(plugin_id, done, self._where())

    def setup(self, launch: PluginLaunch, section: Mapping[str, Any]) -> None:
        done = self._run(launch.id, PluginCommand.SETUP, section)

        self._output.finished(launch.id, PluginCommand.SETUP, done, self._where())

    def launcher_of(
        self, launch: PluginLaunch, contexts: CallContexts, ambient: CallAmbient
    ) -> ToolLauncher:
        program = (*self._program(launch.id), PluginCommand.CALL.value)

        return ProcessToolCaller(
            launch.id, self._cfg, str(self._workdir), contexts, ambient, program
        )

    def settle(self) -> None:
        return

    def stop(self) -> None:
        return

    def _program(self, plugin_id: str) -> tuple[str, ...]:
        return self._entry.argv(sys.executable, plugin_id)

    def _where(self) -> str:
        return f"the host process environment {sys.prefix}"

    def _run(
        self, plugin_id: str, command: PluginCommand, section: Mapping[str, Any]
    ) -> subprocess.CompletedProcess[bytes]:
        argv = (*self._program(plugin_id), command.value)
        try:
            return subprocess.run(  # noqa: S603 — argv: интерпретатор и модуль программы
                argv,
                input=self._output.section_bytes(section),
                capture_output=True,
                timeout=self._cfg.timeout_sec,
                cwd=self._workdir,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            msg = (
                f"plugin {plugin_id}: `{command}` did not finish in "
                f"{self._cfg.timeout_sec}s"
            )
            raise RuntimeError(msg) from exc
        except OSError as exc:
            msg = f"plugin {plugin_id}: `{command}` did not start: {exc}"
            raise RuntimeError(msg) from exc


class ToolLaunchers:
    """Сборка способа запуска по секции [tool_launcher]. Процесс без этой
    секции плагинов инструментов не исполняет (NoLaunchers).

    Создаётся из сырого конфига: загрузкой конфига — чтобы проверить
    предпосылки способа запуска на старте, и корнем сборки приложения —
    чтобы построить способ запуска процесса, один на все сборки
    инструментов.
    """

    SECTION: ClassVar[str] = "tool_launcher"

    def __init__(self, raw: DictConfig) -> None:
        self._raw = raw

    def build(self) -> SectionLaunchers:
        raw = self._raw
        node = OmegaConf.select(raw, self.SECTION)
        if node is None:
            return NoLaunchers()

        section = bind(raw, self.SECTION, ToolLauncherSection).root

        match section:
            case SandboxLauncherConfig():
                return ZygoteLaunchers(raw)
            case ProcessLauncherConfig():
                instance = bind(raw, InstanceConfig.SECTION, InstanceConfig)
                return ProcessLaunchers(section, instance.data(InstanceData.WORKSPACE))
