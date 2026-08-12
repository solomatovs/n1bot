"""Профиль песочницы для тестов пакета: артефакты сборки плюс код репозитория.

Зависимости берутся из собранного site, а сам код инструментов — из src:
иначе тест проверял бы прошлую сборку, а не то, что сейчас в репозитории.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, Protocol

import pytest
from pydantic import BaseModel

from boba.sandbox import SandboxCaller, SandboxToolConfig
from boba.sandbox.runner import ToolCallContext
from boba.sandbox.workflow import ArgsEnricher, StageDef, StageRegistry
from boba.stand.journal import CallStand
from boba.toolkit.channels import ChannelSink, StreamKey
from boba.toolkit.launcher import ChannelHead, LauncherFactory, ToolLauncher
from boba.toolkit.workflow import (
    StageContract,
    StageOutcome,
    WorkflowOutcome,
    WorkflowSpec,
)

REPO = Path(__file__).resolve().parents[4]
SANDBOX = REPO / "build" / "src" / "sandbox"
ROOTFS = SANDBOX / "rootfs"

SRC_PACKAGES = (
    "core/boba-cancellation",
    "core/boba-indexing",
    "core/boba-toolkit",
    "infra/sandbox/boba-sandbox",
    "infra/db/boba-db-postgres",
    "infra/db/boba-db-pgvector",
    "infra/krb/boba-krb",
    "infra/transport/boba-web",
    "infra/transport/boba-transport-http",
    "infra/format/boba-liteparse",
    "infra/format/boba-text",
    "tools/boba-tool-shell",
    "tools/boba-tool-chart",
    "tools/boba-tool-web",
    "tools/boba-tool-doc",
    "tools/boba-tool-postgres",
    "tools/boba-tool-knowledge",
)
"""Код пакетов монтируется одним каталогом: точку /usr/src несёт rootfs."""

ADDRESS_SPACE = 16 * 1024 * 1024 * 1024
"""RLIMIT_AS профиля парсера: pdfium резервирует ~2.3G независимо от документа."""

needs_sandbox = pytest.mark.skipif(
    shutil.which("bwrap") is None or not (ROOTFS / "bin" / "sh").exists(),
    reason="нет bwrap или артефактов песочницы (собрать: make deps)",
)
needs_userns = pytest.mark.skipif(
    os.geteuid() == 0, reason="под root user namespace ведёт себя иначе"
)


def _bin_dirs() -> list[str]:
    """В тестах каталоги берутся из PATH; в проде их задаёт конфиг."""
    dirs: list[str] = []

    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry.startswith("/"):
            continue

        dirs.append(entry)

    return dirs


class SandboxLayout:
    """Монтирования и env: код пакетов кладётся поверх собранного site."""

    @staticmethod
    def ro_binds(docs_dir: Path | None) -> list[str]:
        site_packages = "/usr/local/lib/python3.11/site-packages"
        binds = [
            f"{SANDBOX / 'third' / 'python'}:/usr/local",
            f"{SANDBOX / 'site'}:{site_packages}",
            f"{SANDBOX / 'third' / 'fastembed'}:/var/cache/fastembed",
            f"{SANDBOX / 'third' / 'tessdata'}:/usr/share/tessdata",
        ]
        binds.append(f"{REPO / 'packages'}:/usr/src")
        if docs_dir is not None:
            binds.append(f"{docs_dir}:/workspace")
        return binds

    @staticmethod
    def python_path() -> str:
        """Свой код: интерпретатор и site-packages стоят на штатных местах."""
        parts = []
        for name in SRC_PACKAGES:
            parts.append(f"/usr/src/{name}/src")

        return ":".join(parts)


def sandbox_profile(docs_dir: Path | None = None, **kw: Any) -> dict[str, Any]:
    """Профиль запуска payload'а — тот же по смыслу, что в конфиге приложения."""
    profile: dict[str, Any] = {
        "rootfs": str(ROOTFS),
        "ro_binds": tuple(SandboxLayout.ro_binds(docs_dir)),
        "rw_binds": (),
        "rw_images": (),
        "image_template": "",
        "launcher": {
            "mount_wait_sec": 10.0,
            "mount_poll_sec": 0.05,
            "shutdown_wait_sec": 5.0,
            "lock_wait_sec": 10.0,
            "copy_chunk_bytes": 1 << 20,
        },
        "binaries": {"dirs": _bin_dirs()},
        "tmpfs": ("/tmp:256M",),  # noqa: S108
        "network": False,
        "env_set": {
            "PYTHONPATH": SandboxLayout.python_path(),
            "HOME": "/tmp",  # noqa: S108
            "LANG": "C.UTF-8",
        },
        "timeout_sec": 120,
        "max_memory_bytes": ADDRESS_SPACE,
        "max_cpu_sec": 120,
        "max_file_size_bytes": 64 * 1024 * 1024,
        "max_open_files": 1024,
        "max_processes": 256,
        "max_output_bytes": 16 * 1024 * 1024,
        "cgroup_base": "",
        "oom_score_adj": 0,
        "cwd": "/tmp",  # noqa: S108
    }
    profile.update(kw)
    return profile


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


@pytest.fixture(scope="session")
def raw_config():
    """Конфиг приложения: проверки идут по нему, а не по выдуманным значениям."""
    config_path = os.environ.get("BOBA_CONFIG_PATH")
    if not config_path:
        pytest.skip("BOBA_CONFIG_PATH не задан")
    from boba.settings import build_app_config

    return build_app_config(config_path=Path(config_path))


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


class StageParts(Protocol):
    """Форма узла, которую отдают инструменты: у каждого пакета свой StageNode."""

    @property
    def contract(self) -> StageContract: ...

    @property
    def entry(self) -> tuple[str, ...]: ...

    @property
    def request(self) -> type[BaseModel]: ...

    @property
    def enrich(self) -> ArgsEnricher: ...


class StageTestRegistry:
    """Реестр стадий тестов: узлы пакета плюс профиль песочницы, как в plugins.py."""

    @staticmethod
    def of(
        nodes: Mapping[str, StageParts],
        profile: dict[str, Any],
    ) -> StageRegistry:
        sandbox = SandboxToolConfig.model_validate({"profile": profile, "override": {}})
        rendered = sandbox.effective()

        defs: dict[str, StageDef] = {}
        for name, node in nodes.items():
            defs[name] = StageDef(
                contract=node.contract,
                profile=rendered,
                entry=node.entry,
                request=node.request,
                enrich=node.enrich,
            )

        return StageRegistry(defs)

    @classmethod
    def caller(
        cls,
        nodes: Mapping[str, StageParts],
        profile: dict[str, Any],
    ) -> SandboxCaller:
        """Исполнитель поверх реестра: права в тестах открыты всем узлам."""
        return SandboxCaller(
            cls.of(nodes, profile),
            lambda _tool: True,
            dict,
            CallStand.journal(),
        )

    @classmethod
    def launchers(
        cls,
        nodes: Mapping[str, StageParts],
        profile: dict[str, Any],
    ) -> LauncherFactory:
        caller = cls.caller(nodes, profile)

        def factory(_tool: str) -> SandboxCaller:
            return caller

        return factory


class RecordingLauncher(ToolLauncher):
    """Исполнитель без песочницы: обогащает args узла и запоминает запрос.

    Проверяет путь фасад -> args узла -> обогатитель -> модель запроса, не
    запуская процессов; продукт узла пуст, квитанция берётся из trailers.
    """

    def __init__(
        self,
        nodes: Mapping[str, StageParts],
        trailers: Mapping[str, BaseModel],
    ) -> None:
        self._nodes = nodes
        self._trailers = trailers
        self.requests: list[BaseModel] = []
        self.args: list[Mapping[str, Any]] = []

    def call(
        self,
        spec: WorkflowSpec,
        sinks: Mapping[str, ChannelSink] | None = None,
    ) -> WorkflowOutcome:
        trailers: dict[str, Any] = {}
        stages: list[StageOutcome] = []

        for node in spec.nodes:
            definition = self._nodes[node.tool]
            request = definition.request.model_validate(definition.enrich(node.args))

            self.args.append(node.args)
            self.requests.append(request)

            trailers[node.id] = self._trailers[node.tool].model_dump(mode="json")
            stages.append(
                StageOutcome(
                    stage=node.id,
                    exit_code=0,
                    duration_ms=0,
                    timed_out=False,
                    killed_by_runner=False,
                    diagnostic="",
                )
            )

        if sinks:
            for sink in sinks.values():
                sink.close()

        return WorkflowOutcome(stages=stages, trailers=trailers)

    def head(self, key: StreamKey, max_bytes: int) -> ChannelHead:
        """Журнала у тестового исполнителя нет: голова канала пуста."""
        return ChannelHead.empty()


@pytest.fixture(autouse=True)
def tool_call_context() -> Iterator[ToolCallContext]:
    """Адрес вызова для журнала: песочница без контекста не запускается."""
    with CallStand.bound() as context:
        yield context
