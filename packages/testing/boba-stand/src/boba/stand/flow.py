"""Стенд графов стадий: профиль песочницы, вклады пакетов и прогон WorkflowRunner.

Профиль собирается из артефактов сборки: интерпретатор и зависимости — из
build/src/sandbox, код пакетов — из репозитория. Узлы приходят вкладами
(StageContribution), поэтому сценарий регистрирует ровно свои инструменты, а
стенд не знает, какие пакеты ему понадобятся. Каталог /workspace монтируется
всем стадиям общим rw-биндом, поэтому файлы стадии читаются с хоста.

Ошибки: StandError — стенд собран неверно (спрошен не лист, обязанный
сорваться граф отработал успешно); WorkflowError и WorkflowStageError — граф
не запущен либо стадия сорвалась; WorkflowPayloadError — стадия сообщила об
ожидаемом отказе конвертом tool_result; CollectorCapacityError и
CollectorRowLimitError — потолки коллектора вызывающего; ChannelError —
строчный поток листа не по контракту NDJSON.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel, ConfigDict, Field

from boba.sandbox.argv import WORKSPACE_MOUNT
from boba.sandbox.profile import SandboxProfile
from boba.sandbox.workflow import StageDef, StageRegistry, WorkflowRunner
from boba.toolkit.channels import ByteText, StreamCodec
from boba.toolkit.launcher import WorkflowPayloadError
from boba.toolkit.workflow import (
    EdgeSpec,
    StageNode,
    StageSpec,
    WorkflowOutcome,
    WorkflowSpec,
)


class StandError(Exception):
    """Стенд собран неверно: спрошен не лист либо граф не сорвался, как обязан."""


class StandPaths:
    """Артефакты сборки песочницы и код пакетов: точки монтирования стенда."""

    REPO: ClassVar[Path] = Path(__file__).resolve().parents[6]
    SANDBOX: ClassVar[Path] = REPO / "build" / "src" / "sandbox"
    ROOTFS: ClassVar[Path] = SANDBOX / "rootfs"
    PACKAGES: ClassVar[Path] = REPO / "packages"

    GUEST_PYTHON: ClassVar[str] = "/opt/python"
    GUEST_SITE: ClassVar[str] = "/opt/site"
    GUEST_SRC: ClassVar[str] = "/opt/src"

    @classmethod
    def third(cls, name: str) -> Path:
        """Каталог собранной зависимости: python, fastembed, tessdata и прочие."""
        return cls.SANDBOX / "third" / name

    @classmethod
    def guest_file(cls, name: str) -> str:
        """Путь файла /workspace глазами стадии; хостовый путь даёт FlowStand."""
        return f"{WORKSPACE_MOUNT}/{name}"

    @classmethod
    def workspace_bind(cls, workspace: Path) -> str:
        """Rw-бинд рабочего каталога стенда: хостовый каталог в /workspace."""
        return f"{workspace}:{WORKSPACE_MOUNT}"

    @classmethod
    def artifacts_missing(cls) -> bool:
        """Нет bwrap либо не собран rootfs: запускать стадии нечем."""
        if shutil.which("bwrap") is None:
            return True

        shell = cls.ROOTFS / "bin" / "sh"

        return not shell.exists()


class SandboxMarks:
    """Маркеры пропуска сценариев: без артефактов и под root песочница не идёт."""

    NEEDS_SANDBOX: ClassVar[pytest.MarkDecorator] = pytest.mark.skipif(
        StandPaths.artifacts_missing(),
        reason="нет bwrap или артефактов песочницы (собрать: make deps)",
    )
    NEEDS_USERNS: ClassVar[pytest.MarkDecorator] = pytest.mark.skipif(
        os.geteuid() == 0,
        reason="под root user namespace ведёт себя иначе",
    )


class StandSandbox(BaseModel):
    """Профиль песочницы стенда: код пакетов из репозитория поверх site.

    packages — пути пакетов репозитория (`tools/boba-tool-shell`), их src
    попадает в PYTHONPATH стадии. Сеть тянет за собой резолвер хоста: без
    /etc/hosts и /etc/resolv.conf стадия не найдёт стенд базы по имени.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    packages: tuple[str, ...] = Field(min_length=1)
    ro_binds: tuple[str, ...] = ()
    rw_binds: tuple[str, ...] = ()
    network: bool = False
    timeout_sec: int = Field(default=60, gt=0)
    max_memory_bytes: int = Field(default=2 * 1024 * 1024 * 1024, gt=0)
    max_cpu_sec: int = Field(default=60, gt=0)
    max_output_bytes: int = Field(default=1 << 20, gt=0)

    RESOLVER: ClassVar[tuple[str, ...]] = (
        "/etc/hosts:/etc/hosts",
        "/etc/resolv.conf:/etc/resolv.conf",
    )

    def python_path(self) -> str:
        """PYTHONPATH стадии: src нужных пакетов, следом собранный site."""
        parts: list[str] = []
        for name in self.packages:
            parts.append(f"{StandPaths.GUEST_SRC}/{name}/src")

        parts.append(StandPaths.GUEST_SITE)

        return ":".join(parts)

    def profile(self) -> SandboxProfile:
        """Профиль, пригодный для реального запуска: тот же по смыслу, что в конфиге."""
        raw: dict[str, Any] = {
            "rootfs": str(StandPaths.ROOTFS),
            "ro_binds": self._ro_binds(),
            "rw_binds": self.rw_binds,
            "rw_images": (),
            "image_template": "",
            "launcher": {
                "mount_wait_sec": 10.0,
                "mount_poll_sec": 0.05,
                "shutdown_wait_sec": 5.0,
                "lock_wait_sec": 10.0,
                "copy_chunk_bytes": 1 << 20,
            },
            "tmpfs": ("/tmp:256M",),  # noqa: S108
            "network": self.network,
            "env_set": {
                "PATH": f"{StandPaths.GUEST_PYTHON}/bin:/usr/local/bin:/usr/bin:/bin",
                "PYTHONHOME": StandPaths.GUEST_PYTHON,
                "PYTHONPATH": self.python_path(),
                "LD_LIBRARY_PATH": f"{StandPaths.GUEST_PYTHON}/lib",
                "HOME": "/tmp",  # noqa: S108
                "LANG": "C.UTF-8",
            },
            "timeout_sec": self.timeout_sec,
            "max_memory_bytes": self.max_memory_bytes,
            "max_cpu_sec": self.max_cpu_sec,
            "max_file_size_bytes": 64 * 1024 * 1024,
            "max_open_files": 1024,
            "max_processes": 256,
            "max_output_bytes": self.max_output_bytes,
            "cgroup_base": "",
            "oom_score_adj": 0,
            "cwd": "/tmp",  # noqa: S108
        }

        return SandboxProfile.model_validate(raw)

    def _ro_binds(self) -> tuple[str, ...]:
        binds: list[str] = [
            f"{StandPaths.third('python')}:{StandPaths.GUEST_PYTHON}",
            f"{StandPaths.SANDBOX / 'site'}:{StandPaths.GUEST_SITE}",
            f"{StandPaths.PACKAGES}:{StandPaths.GUEST_SRC}",
        ]

        binds.extend(self.ro_binds)

        if self.network:
            binds.extend(self.RESOLVER)

        return tuple(binds)


@dataclass(frozen=True)
class StageContribution:
    """Вклад пакета в реестр стенда: его узлы и профиль, с которым они пойдут."""

    nodes: Mapping[str, StageNode]
    profile: SandboxProfile


class AllowAllNodes:
    """Права вне сессии: в реестре стенда только заявленные сценарием узлы."""

    def __call__(self, tool: str, /) -> bool:
        return True


class NoPathVars:
    """Переменные путей профиля: в профилях стенда подстановок нет."""

    def __call__(self) -> Mapping[str, str]:
        return {}


class BytesCollector:
    """Приёмник канала данных листа: байты как есть, без разбора формата."""

    def __init__(self) -> None:
        self._parts: list[bytes] = []

    def feed(self, data: bytes) -> None:
        self._parts.append(data)

    def close(self) -> None:
        return None

    def data(self) -> bytes:
        return b"".join(self._parts)


class FlowRun(BaseModel):
    """Итог прогона графа: процессные итоги стадий и байты каналов листьев."""

    model_config = ConfigDict(frozen=True)

    outcome: WorkflowOutcome
    data: Mapping[str, bytes]

    def bytes_of(self, stage: str) -> bytes:
        """Собранный канал данных листа; не лист — StandError."""
        if stage not in self.data:
            raise StandError(f"stage is not a collected leaf: {stage}")

        return self.data[stage]

    def text(self, stage: str) -> str:
        return self.bytes_of(stage).decode(ByteText.ENCODING, ByteText.ERRORS)

    def rows(self, stage: str) -> Sequence[Mapping[str, Any]]:
        """Строчный поток листа (NDJSON) записями; хвостовой перевод пропускается."""
        rows: list[Mapping[str, Any]] = []
        for line in self.text(stage).splitlines():
            if not line:
                continue

            rows.append(StreamCodec.decode_row(line))

        return tuple(rows)

    def exit_code(self, stage: str) -> int:
        return self.outcome.outcome_of(stage).exit_code


class FlowFailure(BaseModel):
    """Сорванный граф: конверт виновника и процессные итоги всех стадий."""

    model_config = ConfigDict(frozen=True)

    kind: str
    message: str
    outcome: WorkflowOutcome

    def killed(self, stage: str) -> bool:
        """Стадию снял раннер каскадом, а не её собственный сбой."""
        return self.outcome.outcome_of(stage).killed_by_runner

    def exit_code(self, stage: str) -> int:
        return self.outcome.outcome_of(stage).exit_code


class FlowStand:
    """Прогон сценарного графа: узлы и рёбра на вход, байты листьев на выход.

    Каналы данных листьев (узлов без исходящих рёбер) собираются целиком:
    продукт графа виден тесту, а побочные эффекты — через стенды инструментов
    и файлы /workspace.
    """

    def __init__(self, runner: WorkflowRunner, workspace: Path) -> None:
        self._runner = runner
        self._workspace = workspace

    @classmethod
    def of(
        cls,
        contributions: Sequence[StageContribution],
        workspace: Path,
    ) -> FlowStand:
        """Реестр из вкладов сценария: имя узла -> узел пакета плюс его профиль."""
        defs: dict[str, StageDef] = {}
        for contribution in contributions:
            for name, node in contribution.nodes.items():
                defs[name] = StageDef.of(node, contribution.profile)

        runner = WorkflowRunner(StageRegistry(defs), AllowAllNodes(), NoPathVars())

        return cls(runner, workspace)

    def run(
        self,
        nodes: Sequence[StageSpec],
        edges: Sequence[EdgeSpec] = (),
    ) -> FlowRun:
        """Граф целиком: спека собирается из узлов и рёбер сценария."""
        spec = WorkflowSpec(nodes=tuple(nodes), edges=tuple(edges))

        collectors: dict[str, BytesCollector] = {}
        for stage_id in self._leaves(spec):
            collectors[stage_id] = BytesCollector()

        outcome = self._runner.run(spec, collectors)

        data: dict[str, bytes] = {}
        for stage_id, collector in collectors.items():
            collector.close()
            data[stage_id] = collector.data()

        return FlowRun(outcome=outcome, data=data)

    def failed(
        self,
        nodes: Sequence[StageSpec],
        edges: Sequence[EdgeSpec] = (),
    ) -> FlowFailure:
        """Граф, который обязан сорваться отказом стадии; успех — ошибка сценария."""
        try:
            self.run(nodes, edges)
        except WorkflowPayloadError as exc:
            return FlowFailure(kind=exc.kind, message=str(exc), outcome=exc.outcome)

        raise StandError("graph was expected to fail, but it succeeded")

    def guest_path(self, name: str) -> str:
        """Путь файла /workspace глазами стадии: он и едет в команду узла."""
        return StandPaths.guest_file(name)

    def workspace_file(self, name: str) -> Path:
        """Путь файла на хосте; стадия видит каталог как /workspace."""
        return self._workspace / name

    def workspace_write(self, name: str, data: bytes) -> str:
        """Файл на входе сценария; возвращается путь глазами стадии."""
        self.workspace_file(name).write_bytes(data)

        return StandPaths.guest_file(name)

    def workspace_bytes(self, name: str) -> bytes:
        return self.workspace_file(name).read_bytes()

    def workspace_text(self, name: str) -> str:
        return self.workspace_file(name).read_text(encoding=ByteText.ENCODING)

    @staticmethod
    def _leaves(spec: WorkflowSpec) -> tuple[str, ...]:
        leaves: list[str] = []
        for node in spec.nodes:
            if spec.consumers_of(node.id):
                continue

            leaves.append(node.id)

        return tuple(leaves)
