"""Способ запуска tool-секций: [tool_launcher] выбирает песочницу или процесс хоста.

Проверки старта и сборка исполнителей упакованы в реализации SectionLaunchers:
песочница проверяет cgroup-лимиты профилей и bwrap, процесс хоста — workdir.

Ошибки:
RuntimeError — секция запуска не согласована с конфигом: нет [tool_launcher],
    [sandbox] или [tool.<name>.sandbox], bwrap недоступен, workdir отсутствует.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Annotated, ClassVar, Literal, Protocol

from omegaconf import DictConfig, OmegaConf
from pydantic import BaseModel, ConfigDict, Field, RootModel

from boba.canvas.journal import CallStream
from boba.canvas.keys import WorkspaceMount
from boba.config import bind
from boba.identity.context import CallContext
from boba.sandbox import (
    BindSpec,
    CgroupManager,
    SandboxProfile,
    SandboxToolConfig,
    has_bwrap,
)
from boba.sandbox.guest import WarmupCall
from boba.sandbox.profile import SandboxConfig
from boba.sandbox.zygote import ZygotePolicy, ZygoteRegistry, ZygoteToolCaller
from boba.toolkit.entry import ToolArgv
from boba.toolkit.facade import WarmupHooks
from boba.toolkit.launcher import ToolLauncher
from boba.toolrun.process import ProcessLauncherConfig, ProcessToolCaller
from boba.toolrun.run_log import NoCallScope
from boba.toolrun.streams import ToolStreams

__all__ = [
    "CallSurface",
    "ProcessLaunchers",
    "SandboxLauncherConfig",
    "SandboxRequire",
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


class SandboxRequire(BaseModel):
    """Кусок секции [sandbox], который читает сборка исполнителей."""

    model_config = ConfigDict(extra="ignore")

    zygote: ZygotePolicy
    """Политика супервизора зигот; каждая sandboxed-секция обслуживается зиготой."""


class CallSurface:
    """Значения из контекста вызова для обвязок запуска инструментов."""

    @staticmethod
    def stream_source(tool: str, call_id: str) -> CallStream | None:
        """Журнал живого вывода вызова; область и субъект — из контекста вызова."""
        if not ToolStreams.streamable(tool):
            return None

        context = CallContext.peek()
        if context is None:
            return None

        return ToolStreams.begin(
            context.subject.user_key, context.scope.id, call_id, tool
        )

    @staticmethod
    def tool_call_scope(call_id: str) -> Callable[[], None]:
        """Контекст вызова инструмента моделью на время вызова: инициатор llm.

        Без контекста ставить нечего — снимать тоже.
        """
        context = CallContext.peek()
        if context is None:
            return NoCallScope.leave

        token = CallContext.push(context.as_tool_call(call_id))

        def leave() -> None:
            CallContext.pop(token)

        return leave

    @staticmethod
    def sandbox_path_vars() -> dict[str, str]:
        """Значения {user_id}/{thread_id} для путей профиля на момент вызова.

        Вне контекста вызова значений нет: профиль с такими переменными
        отказывает рендером, называя недостающую.
        """
        context = CallContext.peek()
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
    def launcher_of(self, section: str, modules: Sequence[str]) -> ToolLauncher:
        """Исполнитель секции [tool.<section>]; её конфиг проверяется здесь."""


class ZygoteLaunchers(SectionLaunchers):
    """Запуск в песочнице: зигота на секцию, профиль из [tool.<name>.sandbox]."""

    SECTION: ClassVar[str] = "sandbox"

    def __init__(self, raw: DictConfig) -> None:
        self._raw = raw

    def probe(self) -> None:
        """Групповые лимиты профилей проверяются на старте: отказ виден сразу,
        с именем профиля.
        """
        node = OmegaConf.select(self._raw, self.SECTION)
        if node is None:
            msg = "[sandbox] is required when [tool_launcher] provider is sandbox"
            raise RuntimeError(msg)

        config = bind(self._raw, self.SECTION, SandboxConfig)
        CgroupManager.probe_profiles(config.profiles)

    def launcher_of(self, section: str, modules: Sequence[str]) -> ToolLauncher:
        profile = self._profile_of(section)

        supervisor = ZygoteRegistry.obtain(
            section,
            profile,
            tuple(modules),
            self._zygote_policy(),
            warmup_calls=self.warmup_configs(section, modules, self._raw),
        )

        return ZygoteToolCaller(
            section, supervisor, profile, CallSurface.sandbox_path_vars
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

    def _zygote_policy(self) -> ZygotePolicy:
        return bind(self._raw, self.SECTION, SandboxRequire).zygote

    def _profile_of(self, section: str) -> SandboxProfile:
        """Профиль секции [tool.<name>.sandbox]; отсутствие — отказ старта.

        Секция есть, а bwrap недоступен — тоже отказ: молчаливая деградация
        инструмента до процесса приложения запрещена.
        """
        node = OmegaConf.select(self._raw, f"tool.{section}.sandbox")
        if node is None:
            msg = f"[tool.{section}.sandbox] is missing: the plugin runs in a sandbox"
            raise RuntimeError(msg)

        sandbox = bind(self._raw, f"tool.{section}.sandbox", SandboxToolConfig)
        profile = sandbox.profile

        if profile.mounts.workspace is not None:
            WorkspaceMount.configure(profile.mounts.workspace.mount.target)

        if not has_bwrap(profile):
            msg = (
                f"[tool.{section}.sandbox] is configured, but bubblewrap (bwrap) "
                "is not in the trusted binary directories"
            )
            raise RuntimeError(msg)

        return profile


class ProcessLaunchers(SectionLaunchers):
    """Запуск субпроцессом хоста: без песочницы, файлы инструментов в workdir."""

    def __init__(self, cfg: ProcessLauncherConfig) -> None:
        self._cfg = cfg

    def probe(self) -> None:
        workdir = Path(self._cfg.workdir)
        if not workdir.is_dir():
            msg = f"[tool_launcher] workdir does not exist: {self._cfg.workdir}"
            raise RuntimeError(msg)

        # файловые ссылки канваса читают файлы инструментов из workdir
        WorkspaceMount.configure(self._cfg.workdir)

    def launcher_of(self, section: str, modules: Sequence[str]) -> ToolLauncher:
        return ProcessToolCaller(section, self._cfg)


class ToolLaunchers:
    """Сборка способа запуска по секции [tool_launcher]."""

    SECTION: ClassVar[str] = "tool_launcher"

    @classmethod
    def of(cls, raw: DictConfig) -> SectionLaunchers:
        node = OmegaConf.select(raw, cls.SECTION)
        if node is None:
            msg = "[tool_launcher] is required: provider = sandbox | process"
            raise RuntimeError(msg)

        section = bind(raw, cls.SECTION, ToolLauncherSection).root

        match section:
            case SandboxLauncherConfig():
                return ZygoteLaunchers(raw)
            case ProcessLauncherConfig():
                return ProcessLaunchers(section)
