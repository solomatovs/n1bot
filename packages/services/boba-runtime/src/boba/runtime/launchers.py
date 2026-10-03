"""Способ запуска tool-секций: [tool_launcher] выбирает песочницу или процесс хоста.

Проверки старта и сборка исполнителей упакованы в реализации SectionLaunchers:
песочница проверяет cgroup-лимиты профилей и bwrap, процесс хоста — workdir.

Ошибки:
RuntimeError — секция запуска не согласована с конфигом: нет [tool_launcher],
    [sandbox] или [tool.<name>.sandbox], bwrap недоступен, workdir отсутствует.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, Protocol

from omegaconf import DictConfig, OmegaConf
from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

from boba.canvas.keys import WorkspaceMount
from boba.config import bind
from boba.identity.context import CallContexts
from boba.sandbox import (
    BindSpec,
    CgroupManager,
    SandboxProfile,
    has_bwrap,
)
from boba.sandbox.guest import WarmupCall
from boba.sandbox.zygote import ZygotePolicy, ZygoteRegistry, ZygoteToolCaller
from boba.toolkit.chain import CallAmbient
from boba.toolkit.entry import ToolArgv
from boba.toolkit.facade import WarmupHooks
from boba.toolkit.launcher import ToolLauncher
from boba.toolkit.manifest import LaunchSpec
from boba.toolrun.process import ProcessLauncherConfig, ProcessToolCaller

__all__ = [
    "CallSurface",
    "PluginSandbox",
    "ProcessLaunchers",
    "SandboxDefaults",
    "SandboxLauncherConfig",
    "SectionLaunchers",
    "ToolLauncherConfig",
    "ToolLaunchers",
    "ZygoteLaunchers",
]


class SandboxLauncherConfig(BaseModel):
    """Секция [tool_launcher] provider = sandbox: запуск в песочнице bwrap.

    Лишние ключи игнорируются: секция общая для всех провайдеров, provider
    выбирается env-переменной.
    """

    model_config = ConfigDict(extra="ignore")

    provider: Literal["sandbox"]


ToolLauncherConfig = Annotated[
    SandboxLauncherConfig | ProcessLauncherConfig,
    Field(discriminator="provider"),
]
"""Discriminated union по provider — точная диагностика ошибок валидации."""


class ToolLauncherSection(RootModel[ToolLauncherConfig]):
    """Секция [tool_launcher] целиком."""


class EnvPaths(BaseModel):
    """Пути развёртывания из [env]: на них стоят конвенции песочницы."""

    model_config = ConfigDict(extra="ignore")

    base: str
    data: str
    sandbox: str
    models: str
    krb: str
    cgroup_base: str


class PluginSandbox(BaseModel):
    """Секция [sandbox] файла conf/plugins/<id>.toml: изоляция плагина.

    profile — полный профиль, отключает сборку. Семантические ключи: network
    включает сеть (по умолчанию её нет), workspace — образ рабочего каталога,
    binds — явные строки host:guest хостовых файлов (resolv.conf, hosts,
    krb5.conf), пути развёртывания — интерполяциями ${env.*}. Таблицы
    host/isolation/limits/run/zygote накладываются на собранный дефолт
    полями профиля.
    """

    model_config = ConfigDict(extra="forbid")

    profile: SandboxProfile | None = None
    network: bool = False
    workspace: bool = False
    binds: tuple[str, ...] = ()
    host: dict[str, Any] = Field(default_factory=dict)
    isolation: dict[str, Any] = Field(default_factory=dict)
    limits: dict[str, Any] = Field(default_factory=dict)
    run: dict[str, Any] = Field(default_factory=dict)
    zygote: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _full_or_delta(self) -> PluginSandbox:
        if self.profile is None:
            return self

        deltas = {
            "network": self.network,
            "workspace": self.workspace,
            "binds": self.binds,
            "host": self.host,
            "isolation": self.isolation,
            "limits": self.limits,
            "run": self.run,
            "zygote": self.zygote,
        }
        given: list[str] = []
        for key, value in deltas.items():
            if value:
                given.append(key)

        if given:
            msg = (
                "[sandbox]: a full profile is exclusive with delta fields, "
                f"got profile together with {', '.join(given)}"
            )
            raise ValueError(msg)

        return self


class SandboxDefaults:
    """Дефолтный профиль песочницы: числа здесь, пути — конвенциями от [env].

    Секции [sandbox] в корневом конфиге нет: всё выводимое строится кодом,
    файл плагина описывает только отличия.
    """

    TMP_SIZE: ClassVar[str] = "512M"
    WORKSPACE_TARGET: ClassVar[str] = "/workspace"

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
        "group_memory_bytes": 1073741824,
        "group_swap_bytes": 0,
        "group_cpu_percent": 100,
        "group_cpu_weight": 100,
        "group_pids_max": 256,
        "group_oom_kill_all": True,
    }

    @classmethod
    def profile(cls, env: EnvPaths, package: str) -> dict[str, Any]:
        return {
            "rootfs": f"{env.sandbox}/plugins/{package}/rootfs.ext4",
            "host": {
                "cgroup_base": env.cgroup_base,
                "binaries": {
                    "dirs": [
                        f"{env.base}/third/bin",
                        "/usr/bin",
                        "/usr/sbin",
                        "/bin",
                        "/sbin",
                    ],
                },
                **cls.HOST,
            },
            "mounts": {
                "ro": [],
                "rw": [],
                "tmp": cls.TMP_SIZE,
            },
            "isolation": dict(cls.ISOLATION),
            "limits": dict(cls.LIMITS),
            "run": {"cwd": "/tmp"},  # noqa: S108 — внутри песочницы  # nosec B108
        }

    @classmethod
    def workspace(cls, env: EnvPaths) -> dict[str, Any]:
        return {
            "template": f"{env.sandbox}/workspace.ext4",
            "mount": (f"{env.data}/workspace/{{user_id}}.ext4:{cls.WORKSPACE_TARGET}"),
        }

    @classmethod
    def merged(
        cls, base: dict[str, Any], override: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Рекурсивное наложение таблиц файла плагина на дефолтный профиль."""
        result = dict(base)
        for key, value in override.items():
            below = result.get(key)
            if isinstance(below, dict) and isinstance(value, Mapping):
                result[key] = cls.merged(below, value)
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
        """Значения {user_id}/{thread_id} для путей профиля на момент вызова.

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
    """Способ запуска tool-секций: проверки старта и исполнители секций."""

    @abstractmethod
    def probe(self) -> None:
        """Проверяет предпосылки способа запуска; нарушение — отказ старта."""

    @abstractmethod
    def launcher_of(
        self, spec: LaunchSpec, contexts: CallContexts, ambient: CallAmbient
    ) -> ToolLauncher:
        """Исполнитель секции спеки; её конфиг и изоляция проверяются здесь.
        contexts — держатель контекста вызова: по нему исполнитель узнаёт
        пользователя и область на момент вызова."""

    @abstractmethod
    def stop(self) -> None:
        """Гасит всё, что способ запуска держит между вызовами; зовётся на
        остановке процесса."""


class ZygoteLaunchers(SectionLaunchers):
    """Запуск в песочнице: зигота на секцию, профиль собирается из [env],
    дефолтов и секции [sandbox] файла плагина.

    Реализация SectionLaunchers; объект один на процесс, зиготы секций живут
    в его реестре (ZygoteRegistry) между сборками инструментов."""

    def __init__(self, raw: DictConfig) -> None:
        self._raw = raw
        self._zygotes = ZygoteRegistry()

    def stop(self) -> None:
        self._zygotes.stop_all()

    def probe(self) -> None:
        """Пути конвенций проверяются разбором [env]; cgroup-лимиты каждой
        секции проверяет сборка её профиля.
        """
        self._env()

    def _env(self) -> EnvPaths:
        return bind(self._raw, "env", EnvPaths)

    def launcher_of(
        self, spec: LaunchSpec, contexts: CallContexts, ambient: CallAmbient
    ) -> ToolLauncher:
        profile = self.profile_of(spec)
        sandbox = self._plugin_sandbox(spec.section)

        supervisor = self._zygotes.obtain(
            spec.section,
            profile,
            tuple(spec.modules),
            ZygotePolicy.model_validate(sandbox.zygote),
            warmup_calls=self.warmup_configs(spec.section, spec.modules, self._raw),
        )

        surface = CallSurface(contexts)

        return ZygoteToolCaller(
            spec.section, supervisor, profile, ambient, surface.sandbox_path_vars
        )

    @staticmethod
    def warmup_configs(
        section: str, modules: Sequence[str], raw_config: DictConfig
    ) -> tuple[WarmupCall, ...]:
        """Прогревы модулей секции: конфиг каждому хуку из секции его инструмента.

        Хуки объявляет автор инструмента через @warmup, хост их не угадывает —
        берёт из реестра фасада. Модель конфига — аннотация параметра хука,
        значения приезжают из [tool.<section>].
        """
        calls: list[WarmupCall] = []

        for name in modules:
            for hook in WarmupHooks.of(name):
                model = bind(raw_config, f"tool.{section}", hook.config_model)
                calls.append(
                    WarmupCall(
                        module=name,
                        hook=hook.name,
                        config=ToolArgv.reveal(hook.config_model, model),
                    )
                )

        return tuple(calls)

    def _plugin_sandbox(self, section: str) -> PluginSandbox:
        return bind(self._raw, f"tool.{section}.sandbox", PluginSandbox)

    def profile_of(self, spec: LaunchSpec) -> SandboxProfile:
        """Профиль секции по [sandbox] файла плагина: полный профиль как есть,
        иначе дефолты от [env] плюс образ пакета и дельта изоляции.

        Профиль есть, а bwrap недоступен — отказ старта: молчаливая деградация
        инструмента до процесса приложения запрещена.
        """
        sandbox = self._plugin_sandbox(spec.section)
        if sandbox.profile is not None:
            return self._checked(sandbox.profile, spec.section)

        return self._checked(self._composed(spec, sandbox), spec.section)

    def _checked(self, profile: SandboxProfile, section: str) -> SandboxProfile:
        if profile.mounts.workspace is not None:
            WorkspaceMount.configure(profile.mounts.workspace.mount.target)

        if not has_bwrap(profile):
            msg = (
                f"sandbox profile of {section!r} is configured, but bubblewrap "
                "(bwrap) is not in the trusted binary directories"
            )
            raise RuntimeError(msg)

        # групповые лимиты секции проверяются при сборке: отказ виден на старте
        CgroupManager.probe_profiles({section: profile})
        return profile

    def _composed(self, spec: LaunchSpec, needs: PluginSandbox) -> SandboxProfile:
        """Дефолтный профиль от [env] + образ plugins/<package>/rootfs.ext4 +
        семантика и таблицы из [sandbox] файла conf/plugins/<section>.toml.
        """
        if not spec.package:
            msg = (
                f"[tool.{spec.section}.sandbox] profile is missing: a built-in "
                "plugin declares the full sandbox profile in its config"
            )
            raise RuntimeError(msg)

        env = self._env()
        data = SandboxDefaults.profile(env, spec.package)

        ro = list(data["mounts"]["ro"])

        if needs.network:
            data["isolation"]["network"] = True

        for entry in needs.binds:
            if ":" not in entry:
                msg = (
                    f"plugin {spec.section!r}: bind {entry!r} must be an "
                    "explicit host:guest pair"
                )
                raise RuntimeError(msg)

            ro.append(entry)

        data["mounts"]["ro"] = ro

        if needs.workspace:
            data["mounts"]["workspace"] = SandboxDefaults.workspace(env)
            data["run"]["cwd"] = SandboxDefaults.WORKSPACE_TARGET

        overrides = {
            "host": needs.host,
            "isolation": needs.isolation,
            "limits": needs.limits,
            "run": needs.run,
        }
        for key, override in overrides.items():
            if override:
                data[key] = SandboxDefaults.merged(data[key], override)

        return SandboxProfile.model_validate(data)


class ProcessLaunchers(SectionLaunchers):
    """Запуск субпроцессом хоста: без песочницы, файлы инструментов в workdir."""

    def __init__(self, cfg: ProcessLauncherConfig) -> None:
        self._cfg = cfg

    def probe(self) -> None:
        workdir = Path(self._cfg.workdir)
        if not workdir.is_dir():
            msg = (
                "[tool_launcher] provider = process: workdir must be an existing "
                f"directory, got {self._cfg.workdir!r}"
            )
            raise RuntimeError(msg)

        # файловые ссылки канваса читают файлы инструментов из workdir
        WorkspaceMount.configure(self._cfg.workdir)

    def launcher_of(
        self, spec: LaunchSpec, contexts: CallContexts, ambient: CallAmbient
    ) -> ToolLauncher:
        return ProcessToolCaller(spec.section, self._cfg, contexts, ambient)

    def stop(self) -> None:
        return


class ToolLaunchers:
    """Сборка способа запуска по секции [tool_launcher].

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
            msg = (
                "[tool_launcher] is required but the config has no such section: "
                "expected provider = sandbox | process"
            )
            raise RuntimeError(msg)

        section = bind(raw, self.SECTION, ToolLauncherSection).root

        match section:
            case SandboxLauncherConfig():
                return ZygoteLaunchers(raw)
            case ProcessLauncherConfig():
                return ProcessLaunchers(section)
