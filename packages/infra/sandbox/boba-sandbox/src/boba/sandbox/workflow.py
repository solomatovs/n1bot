"""Исполнение графа стадий: реестр узлов, валидация с mount-группами, WorkflowRunner.

Ошибки: WorkflowError — спецификация или контракт нарушены, стадия сорвалась,
итог не собран; PayloadFailureError — стадия сообщила об ожидаемом отказе
конвертом tool_result; исключения sink'ов пути данных (коллекторов
вызывающего) проходят наверх как есть — их контракт задаёт потребитель;
ToolStopped (BaseException остановки хода) проходит насквозь.
"""

from __future__ import annotations

import logging
import os
import resource
import shlex
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Final

from pydantic import BaseModel, JsonValue, ValidationError

from boba.cancellation import TurnCancellation, current_cancellation
from boba.sandbox.argv import ChannelArgv, WrapArgsCodec
from boba.sandbox.cgroup import CgroupError, CgroupManager, GroupLimits
from boba.sandbox.diagnostics import FailureFacts, SandboxDiagnostics
from boba.sandbox.profile import BindSpec, SandboxProfile
from boba.sandbox.runner import (
    ChannelPump,
    FanoutSink,
    KillCause,
    PipeSink,
    RelaySink,
    ResultSink,
    SandboxChannels,
    TailSink,
    WrapResultSink,
)
from boba.toolkit.channels import (
    Channel,
    ChannelSink,
    ResultError,
    ShellExit,
    ValidationSummary,
)
from boba.toolkit.launcher import (
    CollectorCapacityError,
    CollectorRowLimitError,
    LauncherError,
    PayloadFailureError,
)
from boba.toolkit.payload import PayloadLogging
from boba.toolkit.workflow import (
    AccessPredicate,
    ArgsEnricher,
    ContractCheck,
    EdgeSpec,
    OutResolver,
    StageContract,
    StageNode,
    StageOutcome,
    StageSpec,
    WorkflowError,
    WorkflowOutcome,
    WorkflowSpec,
)
from boba.workspace.launcher import (
    ChainChannelArgv,
    LauncherExit,
    LauncherMode,
    LauncherOptions,
    MountError,
    ResourceLimits,
    StageEntry,
    require_fuse,
)

__all__ = [
    "GroupIds",
    "ImageRef",
    "MountGroupPlan",
    "StageDef",
    "StagePlan",
    "StageRegistry",
    "ToolArgsField",
    "WorkflowRunner",
]

logger = logging.getLogger(__name__)


class ToolArgsField:
    """Ключи, которые раннер добавляет в tool_args поверх обогащения."""

    STDIN_FORMAT: Final = "stdin_format"


@dataclass(frozen=True)
class StageDef:
    """Описание узла реестра стадий: всё, что нужно раннеру для запуска.

    out_of — формат продукта как функция запроса; None — константа contract.out.
    Модель запроса читающего узла обязана проносить поле stdin_format: иначе
    инжект раннера молча выпал бы из model_dump_json при extra='ignore'.
    """

    contract: StageContract
    profile: SandboxProfile
    entry: tuple[str, ...]
    request: type[BaseModel]
    enrich: ArgsEnricher
    out_of: OutResolver | None = None

    @classmethod
    def of(cls, node: StageNode, profile: SandboxProfile) -> StageDef:
        """Узел пакета плюс профиль песочницы инструмента-владельца."""
        return cls(
            contract=node.contract,
            profile=profile,
            entry=node.entry,
            request=node.request,
            enrich=node.enrich,
            out_of=node.out_of,
        )

    def __post_init__(self) -> None:
        if not self.entry:
            raise WorkflowError("stage definition has an empty entry command")

        if not self.contract.accepts:
            return

        if self._carries_stdin_format():
            return

        msg = (
            f"request model {self.request.__name__} of a consuming stage "
            f"must carry the {ToolArgsField.STDIN_FORMAT!r} field"
        )
        raise WorkflowError(msg)

    def _carries_stdin_format(self) -> bool:
        if ToolArgsField.STDIN_FORMAT in self.request.model_fields:
            return True

        return self.request.model_config.get("extra") == "allow"


class StageRegistry:
    """Реестр стадий: имя узлового инструмента -> StageDef."""

    def __init__(self, defs: Mapping[str, StageDef]) -> None:
        self._defs = dict(defs)

    @classmethod
    def empty(cls) -> StageRegistry:
        """Пустой реестр: боевой состав узлов появляется на этапе 3."""
        return cls({})

    def names(self) -> tuple[str, ...]:
        return tuple(self._defs)

    def def_of(self, tool: str) -> StageDef:
        """Описание узла; неизвестное имя — WorkflowError."""
        definition = self._defs.get(tool)
        if definition is None:
            raise WorkflowError(f"unknown workflow tool: {tool}")

        return definition


class ImageRef:
    """Идентичность rw-образа: канонический путь, inode-ключ, точка монтирования."""

    MNT_SUFFIX: ClassVar[str] = ".mnt"

    @classmethod
    def canonical(cls, host: str) -> str:
        return os.path.realpath(host)

    @classmethod
    def key_of(cls, host: str) -> str:
        """Ключ группировки: (st_dev, st_ino); несозданный образ — по realpath."""
        real = cls.canonical(host)

        try:
            info = os.stat(real)
        except FileNotFoundError:
            return f"path:{real}"

        return f"inode:{info.st_dev}:{info.st_ino}"

    @classmethod
    def mount_point(cls, host: str) -> str:
        return cls.canonical(host) + cls.MNT_SUFFIX


class GroupIds:
    """Служебные id mount-групп: g1, g2, … — первый свободный от id узлов."""

    PREFIX: ClassVar[str] = "g"

    @classmethod
    def allocate(cls, taken: Iterable[str], count: int) -> tuple[str, ...]:
        busy = set(taken)

        ids: list[str] = []
        ordinal = 1
        while len(ids) < count:
            candidate = f"{cls.PREFIX}{ordinal}"
            ordinal += 1
            if candidate in busy:
                continue
            ids.append(candidate)

        return tuple(ids)


@dataclass(frozen=True)
class StagePlan:
    """Стадия после валидации: разрешённый контракт, срендеренный профиль, запрос."""

    spec: StageSpec
    definition: StageDef
    contract: StageContract
    rendered: SandboxProfile
    request_json: str
    limits: ResourceLimits
    literal: bytes

    @property
    def label(self) -> str:
        return f"{self.spec.tool}:{self.spec.id}"


@dataclass(frozen=True)
class MountGroupPlan:
    """Mount-группа: стадии общих rw-образов под одной цепочкой лаунчера."""

    group_id: str
    stage_ids: tuple[str, ...]
    images: tuple[tuple[str, str], ...]
    template: str
    options: LauncherOptions
    envelope: ResourceLimits
    network: bool
    rw_paths: tuple[str, ...]


class _UnionFind:
    """Компоненты связности стадий по общим rw-образам."""

    def __init__(self, items: Iterable[str]) -> None:
        self._parent: dict[str, str] = {}
        for item in items:
            self._parent[item] = item

    def find(self, item: str) -> str:
        root = item
        while self._parent[root] != root:
            root = self._parent[root]

        while self._parent[item] != root:
            self._parent[item], item = root, self._parent[item]

        return root

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)

        if left_root != right_root:
            self._parent[right_root] = left_root


@dataclass
class _EdgeLine:
    """Ребро в исполнении: read-конец для stdin приёмника и PipeSink насоса.

    Write-концом владеет насос (`add_edge`); свой read-конец родитель
    закрывает после запуска всех стадий.
    """

    spec: EdgeSpec
    read_fd: int
    pipe: PipeSink
    read_open: bool = True

    def close_read(self) -> None:
        if not self.read_open:
            return

        os.close(self.read_fd)
        self.read_open = False


class _GroupResultWatch(ChannelSink):
    """wrap_result группы: копит отчёты и зовёт хук на каждом новом rc стадии.

    Хук решает судьбу группы (kill внешнего процесса при сбойном rc); правило
    rc 141 применяется в нём же по признакам рёбер. Sink пути результата:
    битая строка канала фатальна для запуска.
    """

    def __init__(
        self,
        inner: WrapResultSink,
        on_exit: Callable[[str, int], None],
    ) -> None:
        self._inner = inner
        self._on_exit = on_exit
        self._seen: set[str] = set()

    def feed(self, data: bytes) -> None:
        self._inner.feed(data)

        self._scan()

    def close(self) -> None:
        self._inner.close()

        self._scan()

    def exits(self) -> dict[str, int]:
        return self._inner.exits()

    def _scan(self) -> None:
        for stage, rc in self._inner.exits().items():
            if stage in self._seen:
                continue
            self._seen.add(stage)
            self._on_exit(stage, rc)


@dataclass(frozen=True)
class _LaunchPlan:
    """Собранный запуск обычной стадии: argv, подача wrap-каналов, способ лимитов."""

    argv: tuple[str, ...]
    wrap_feeds: Mapping[Channel, bytes]
    runner_limits: ResourceLimits | None


@dataclass
class _StageRun:
    """Рантайм стадии: sink'и итога и процесс (None у стадии mount-группы)."""

    plan: StagePlan
    result: ResultSink[BaseModel]
    stdout_tail: TailSink
    stderr_tail: TailSink
    wrap_out_tail: TailSink | None
    wrap_err_tail: TailSink | None
    proc: subprocess.Popen[bytes] | None
    group_id: str


@dataclass
class _GroupRun:
    """Рантайм mount-группы: wrap-хвосты, отчёты стадий и внешний процесс."""

    plan: MountGroupPlan
    wrap_out_tail: TailSink
    wrap_err_tail: TailSink
    watch: _GroupResultWatch
    proc: subprocess.Popen[bytes]


@dataclass(frozen=True)
class _Taps:
    """Дополнительные приёмники каналов по id узла.

    payload — коллекторы вызывающего и журнал (этап 4); stdout — временный
    tap живого потока панели (этап 3, умирает вместе с ToolStreamTap).
    """

    payload: Mapping[str, ChannelSink]
    stdout: Mapping[str, ChannelSink]


@dataclass(frozen=True)
class _MemberBoot:
    """Отложенная регистрация стадии группы: pump.add после спавна внешнего процесса."""

    stage_id: str
    channels: SandboxChannels
    sinks: Mapping[Channel, ChannelSink]
    feeds: Mapping[Channel, bytes]
    timeout_sec: float


class WorkflowRunner:
    """Исполнение WorkflowSpec: валидация, mount-группы, веер, один насос на граф."""

    ENCODING: ClassVar[str] = "utf-8"
    FD_HEADROOM: ClassVar[int] = 32
    FAIL_TAIL_CHARS: ClassVar[int] = 2000
    TAIL_BYTES: ClassVar[int] = 65536
    """Потолок хвостов stderr и wrap-каналов: текст для диагностики и ошибок."""

    APP_LOGGER: ClassVar[str] = "boba"
    """Чей уровень наследует payload: настройка живёт в конфиге приложения."""

    GROUP_CHANNELS: ClassVar[tuple[Channel, ...]] = (
        Channel.WRAP_ARGS,
        Channel.WRAP_STDOUT,
        Channel.WRAP_STDERR,
        Channel.WRAP_RESULT,
    )

    def __init__(
        self,
        registry: StageRegistry,
        access: AccessPredicate,
        path_vars: Callable[[], Mapping[str, str]],
    ) -> None:
        self._registry = registry
        self._access = access
        self._path_vars = path_vars

    @staticmethod
    def cgroup_preexec(cgroup_dir: str | None) -> Callable[[], None] | None:
        """Вход в cgroup до exec: всё дерево стадии рождается уже внутри leaf'а."""
        if cgroup_dir is None:
            return None

        procs_path = os.path.join(cgroup_dir, "cgroup.procs")

        def enter_cgroup() -> None:
            fd = os.open(procs_path, os.O_WRONLY)
            os.write(fd, b"0")
            os.close(fd)

        return enter_cgroup

    def run(
        self,
        spec: WorkflowSpec,
        taps: Mapping[str, ChannelSink] | None = None,
        stdout_taps: Mapping[str, ChannelSink] | None = None,
    ) -> WorkflowOutcome:
        """Полная валидация до старта, одновременный запуск стадий, сбор итога.

        taps — дополнительные приёмники tool_payload по id узла (коллекторы
        вызывающего; журнал — этап 4); stdout_taps — временные приёмники
        tool_stdout (живой поток панели, этап 3).
        """
        cancellation = current_cancellation()

        if taps is None:
            taps = {}

        if stdout_taps is None:
            stdout_taps = {}

        bundle = _Taps(payload=taps, stdout=stdout_taps)

        try:
            plans = self._plan(spec)

            self._check_contracts(spec, plans)

            groups = self._grouped(spec, plans)

            self._check_fd_budget(spec, plans, groups)

            return self._execute(spec, plans, groups, bundle, cancellation)
        except WorkflowError:
            raise
        except PayloadFailureError:
            raise
        except (CollectorCapacityError, CollectorRowLimitError):
            raise
        except LauncherError as exc:
            raise WorkflowError(f"workflow execution failed: {exc}") from exc
        except Exception as exc:
            raise WorkflowError(f"workflow internal failure: {exc}") from exc

    def _plan(self, spec: WorkflowSpec) -> dict[str, StagePlan]:
        """Стадии в топологическом порядке: обогащение, валидация, рендер профиля."""
        plans: dict[str, StagePlan] = {}

        for stage_id in spec.order():
            node = spec.stage(stage_id)
            plans[stage_id] = self._plan_stage(spec, node, plans)

        return plans

    def _plan_stage(
        self,
        spec: WorkflowSpec,
        node: StageSpec,
        plans: Mapping[str, StagePlan],
    ) -> StagePlan:
        definition = self._registry.def_of(node.tool)

        enriched = self._enriched_args(spec, node, definition, plans)

        request = self._validated_request(node, definition, enriched)

        contract = self._resolved_contract(definition, request)

        rendered = self._rendered_profile(node, definition)

        literal = b""
        if node.stdin is not None:
            literal = node.stdin.encode(self.ENCODING)

        return StagePlan(
            spec=node,
            definition=definition,
            contract=contract,
            rendered=rendered,
            request_json=request.model_dump_json(),
            limits=rendered.limits(),
            literal=literal,
        )

    def _enriched_args(
        self,
        spec: WorkflowSpec,
        node: StageSpec,
        definition: StageDef,
        plans: Mapping[str, StagePlan],
    ) -> dict[str, Any]:
        """Обогащённые args: значения не обязаны быть JSON, профиль остаётся моделью."""
        try:
            enriched = dict(definition.enrich(node.args))
        except Exception as exc:
            # текст чужой ошибки может нести значения args (секреты): наружу тип
            msg = f"stage {node.id}: args enrichment failed: {type(exc).__name__}"
            raise WorkflowError(msg) from exc

        source = spec.source_of(node.id)
        if source is None:
            return enriched

        # формат входа приёмнику кладёт приложение: payload не нюхает байты
        src_out = plans[source].contract.out
        if src_out is not None:
            enriched[ToolArgsField.STDIN_FORMAT] = src_out.value

        return enriched

    @staticmethod
    def _validated_request(
        node: StageSpec,
        definition: StageDef,
        enriched: Mapping[str, Any],
    ) -> BaseModel:
        try:
            return definition.request.model_validate(enriched)
        except ValidationError as exc:
            # сводка без значений полей: обогащённые args несут секреты
            summary = ValidationSummary.of(exc)
            msg = (
                f"stage {node.id}: args do not match "
                f"{definition.request.__name__}: {summary}"
            )
            raise WorkflowError(msg) from exc

    @staticmethod
    def _resolved_contract(
        definition: StageDef,
        request: BaseModel,
    ) -> StageContract:
        if definition.out_of is None:
            return definition.contract

        out = definition.out_of(request)

        return definition.contract.model_copy(update={"out": out})

    def _rendered_profile(
        self,
        node: StageSpec,
        definition: StageDef,
    ) -> SandboxProfile:
        try:
            rendered = definition.profile.render(dict(self._path_vars()))
            self._prepare_dirs(rendered)
        except Exception as exc:
            msg = f"stage {node.id}: profile is not runnable: {exc}"
            raise WorkflowError(msg) from exc

        return rendered

    @staticmethod
    def _prepare_dirs(profile: SandboxProfile) -> None:
        for bind in profile.rw_binds:
            Path(bind.host).mkdir(parents=True, exist_ok=True)

        for bind in profile.rw_images:
            Path(bind.host).parent.mkdir(parents=True, exist_ok=True)

    def _check_contracts(
        self,
        spec: WorkflowSpec,
        plans: Mapping[str, StagePlan],
    ) -> None:
        contracts: dict[str, StageContract] = {}
        for stage_id, plan in plans.items():
            contracts[stage_id] = plan.contract

        ContractCheck(contracts, self._access).check(spec)

    def _grouped(
        self,
        spec: WorkflowSpec,
        plans: Mapping[str, StagePlan],
    ) -> tuple[MountGroupPlan, ...]:
        """Компоненты связности по общим rw-образам; одиночки остаются цепочкой."""
        imaged: list[str] = []
        for stage_id, plan in plans.items():
            if plan.rendered.rw_images:
                imaged.append(stage_id)

        if not imaged:
            return ()

        components = self._components(imaged, plans)

        multi: list[tuple[str, ...]] = []
        for component in components:
            if len(component) > 1:
                multi.append(component)

        if not multi:
            return ()

        node_ids: list[str] = []
        for node in spec.nodes:
            node_ids.append(node.id)

        group_ids = GroupIds.allocate(node_ids, len(multi))

        groups: list[MountGroupPlan] = []
        for group_id, members in zip(group_ids, multi, strict=True):
            groups.append(self._group_plan(group_id, members, plans))

        return tuple(groups)

    @staticmethod
    def _components(
        imaged: Sequence[str],
        plans: Mapping[str, StagePlan],
    ) -> tuple[tuple[str, ...], ...]:
        forest = _UnionFind(imaged)

        first_by_key: dict[str, str] = {}
        for stage_id in imaged:
            for bind in plans[stage_id].rendered.rw_images:
                key = ImageRef.key_of(bind.host)
                owner = first_by_key.get(key)
                if owner is None:
                    first_by_key[key] = stage_id
                    continue
                forest.union(owner, stage_id)

        by_root: dict[str, list[str]] = {}
        for stage_id in imaged:
            by_root.setdefault(forest.find(stage_id), []).append(stage_id)

        components: list[tuple[str, ...]] = []
        for members in by_root.values():
            components.append(tuple(members))

        return tuple(components)

    def _group_plan(
        self,
        group_id: str,
        members: tuple[str, ...],
        plans: Mapping[str, StagePlan],
    ) -> MountGroupPlan:
        templates: set[str] = set()
        options: set[LauncherOptions] = set()
        for stage_id in members:
            templates.add(plans[stage_id].rendered.image_template)
            options.add(plans[stage_id].rendered.launcher.to_options())

        if len(templates) != 1:
            msg = (
                f"mount group {group_id}: stages must share image_template, "
                f"got {sorted(templates)}"
            )
            raise WorkflowError(msg)

        if len(options) != 1:
            msg = f"mount group {group_id}: stages must share launcher options"
            raise WorkflowError(msg)

        images: dict[str, tuple[str, str]] = {}
        for stage_id in members:
            for bind in plans[stage_id].rendered.rw_images:
                key = ImageRef.key_of(bind.host)
                if key in images:
                    continue
                images[key] = (
                    ImageRef.canonical(bind.host),
                    ImageRef.mount_point(bind.host),
                )

        network = False
        rw_paths: dict[str, None] = {}
        member_limits: list[ResourceLimits] = []
        for stage_id in members:
            rendered = plans[stage_id].rendered
            if rendered.network:
                network = True
            for bind in rendered.rw_binds:
                rw_paths[bind.host] = None
            member_limits.append(plans[stage_id].limits)

        return MountGroupPlan(
            group_id=group_id,
            stage_ids=members,
            images=tuple(images.values()),
            template=next(iter(templates)),
            options=next(iter(options)),
            envelope=self._envelope_limits(member_limits),
            network=network,
            rw_paths=tuple(rw_paths),
        )

    @staticmethod
    def _envelope_limits(items: Sequence[ResourceLimits]) -> ResourceLimits:
        """Огибающая rlimit'ов группы: максимум по каждому ресурсу."""
        memory = 0
        cpu = 0
        fsize = 0
        nofile = 0
        oom = 0
        for limits in items:
            memory = max(memory, limits.max_memory_bytes)
            cpu = max(cpu, limits.max_cpu_sec)
            fsize = max(fsize, limits.max_file_size_bytes)
            nofile = max(nofile, limits.max_open_files)
            oom = max(oom, limits.oom_score_adj)

        return ResourceLimits(
            max_memory_bytes=memory,
            max_cpu_sec=cpu,
            max_file_size_bytes=fsize,
            max_open_files=nofile,
            oom_score_adj=oom,
        )

    def _check_fd_budget(
        self,
        spec: WorkflowSpec,
        plans: Mapping[str, StagePlan],
        groups: Sequence[MountGroupPlan],
    ) -> None:
        """Потребность в дескрипторах против RLIMIT_NOFILE: падать здесь, не на pipe."""
        members: set[str] = set()
        for group in groups:
            members.update(group.stage_ids)

        fed: set[str] = set()
        for edge in spec.edges:
            fed.add(edge.dst)

        pairs = 0
        for stage_id, plan in plans.items():
            wanted = self._stage_channels(
                plan, member=stage_id in members, edge_fed=stage_id in fed
            )
            pairs += len(wanted)

        pairs += len(self.GROUP_CHANNELS) * len(groups)

        need = 2 * pairs + 2 * len(spec.edges) + self.FD_HEADROOM

        soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        open_now = len(os.listdir("/proc/self/fd"))

        if open_now + need > soft:
            msg = (
                f"workflow needs about {need} descriptors on top of {open_now} "
                f"already open, RLIMIT_NOFILE soft limit is {soft}"
            )
            raise WorkflowError(msg)

    @staticmethod
    def _stage_channels(
        plan: StagePlan,
        *,
        member: bool,
        edge_fed: bool,
    ) -> tuple[Channel, ...]:
        """Фактические pipe-пары стадии; у члена группы wrap-каналы держит группа.

        Вход существует при ребре или литерале: узлу без входа канал tool_stdin
        не создаётся и в env не объявляется.
        """
        wanted: list[Channel] = [
            Channel.TOOL_ARGS,
            Channel.TOOL_STDOUT,
            Channel.TOOL_STDERR,
            Channel.TOOL_RESULT,
        ]

        if not member:
            wanted.insert(0, Channel.WRAP_ARGS)
            wanted.append(Channel.WRAP_STDOUT)
            wanted.append(Channel.WRAP_STDERR)

        if not edge_fed and plan.spec.stdin is not None:
            wanted.append(Channel.TOOL_STDIN)

        if member or plan.rendered.rw_images:
            wanted.append(Channel.WRAP_ARGS_INNER)

        if plan.contract.out is not None:
            wanted.append(Channel.TOOL_PAYLOAD)

        return tuple(wanted)

    def _execute(
        self,
        spec: WorkflowSpec,
        plans: Mapping[str, StagePlan],
        groups: Sequence[MountGroupPlan],
        taps: _Taps,
        cancellation: TurnCancellation,
    ) -> WorkflowOutcome:
        logger.info(
            "workflow: %d stage(s), %d edge(s), %d mount group(s)",
            len(spec.nodes),
            len(spec.edges),
            len(groups),
        )

        group_of: dict[str, MountGroupPlan] = {}
        for group in groups:
            for stage_id in group.stage_ids:
                group_of[stage_id] = group

        runs: dict[str, _StageRun] = {}
        group_runs: dict[str, _GroupRun] = {}

        with ExitStack() as stack:
            pump = stack.enter_context(ChannelPump())

            edges = self._open_edges(spec, pump, stack)

            edge_in: dict[str, _EdgeLine] = {}
            pipes_by_src: dict[str, list[PipeSink]] = {}
            for edge in edges:
                edge_in[edge.spec.dst] = edge
                pipes_by_src.setdefault(edge.spec.src, []).append(edge.pipe)

            launched_groups: set[str] = set()
            for stage_id in spec.order():
                group = group_of.get(stage_id)

                if group is None:
                    self._launch_single(
                        plans[stage_id], edge_in.get(stage_id), pipes_by_src,
                        taps, pump, stack, cancellation, runs,
                    )
                    continue

                if group.group_id in launched_groups:
                    continue

                launched_groups.add(group.group_id)
                self._launch_group(
                    group, plans, edge_in, pipes_by_src, taps,
                    pump, stack, cancellation, runs, group_runs,
                )

            # свой read-конец каждого ребра закрыт: EOF и EPIPE ходят между
            # источником, насосом и приёмником, а не висят на родителе
            for edge in edges:
                edge.close_read()

            pump.run(cancellation)

        cancellation.raise_if_cancelled()

        return self._assemble(spec, pump, runs, group_runs, pipes_by_src)

    def _open_edges(
        self,
        spec: WorkflowSpec,
        pump: ChannelPump,
        stack: ExitStack,
    ) -> tuple[_EdgeLine, ...]:
        lines: list[_EdgeLine] = []

        for edge in spec.edges:
            try:
                read_fd, write_fd = os.pipe()
            except OSError as exc:
                raise WorkflowError(f"edge pipe failed: {exc}") from exc

            name = self._edge_name(edge)

            try:
                pipe = pump.add_edge(
                    name, write_fd, buffer_limit=ChannelPump.EDGE_BUFFER_BYTES
                )
            except LauncherError:
                os.close(read_fd)
                os.close(write_fd)
                raise

            line = _EdgeLine(spec=edge, read_fd=read_fd, pipe=pipe)
            stack.callback(line.close_read)
            lines.append(line)

        return tuple(lines)

    @staticmethod
    def _edge_name(edge: EdgeSpec) -> str:
        return f"{edge.src}->{edge.dst}"

    def _launch_single(  # noqa: PLR0913
        self,
        plan: StagePlan,
        edge: _EdgeLine | None,
        pipes_by_src: Mapping[str, Sequence[PipeSink]],
        taps: _Taps,
        pump: ChannelPump,
        stack: ExitStack,
        cancellation: TurnCancellation,
        runs: dict[str, _StageRun],
    ) -> None:
        """Обычная стадия: свой bwrap либо fuse-цепочка одной стадии."""
        stage_id = plan.spec.id
        edge_fed = edge is not None

        wanted = self._stage_channels(plan, member=False, edge_fed=edge_fed)
        channels = stack.enter_context(SandboxChannels(wanted))

        channel_env = dict(channels.child_env())
        if edge_fed:
            channel_env[Channel.TOOL_STDIN.env_name] = str(
                SandboxChannels.STDIN_CHILD_FD
            )

        env = self._payload_env(plan.rendered) | channel_env

        launch = self._launch_plan(plan, channels, env, channel_env)

        cgroup_dir = self._acquire_cgroup(plan, stack)

        proc = self._spawn(
            launch.argv,
            stdin_fd=self._stdin_fd(channels, edge),
            stdout_fd=channels.child_fd(Channel.WRAP_STDOUT),
            stderr_fd=channels.child_fd(Channel.WRAP_STDERR),
            pass_fds=channels.child_fds(),
            cgroup_dir=cgroup_dir,
        )
        # процесс захвачен до регистрации прерывателя: ранний ToolStopped
        # разматывает стек, а не теряет процесс
        stack.callback(ChannelPump.reap, proc)
        stack.enter_context(cancellation.abort_with(proc.kill))

        channels.close_child_side()

        if launch.runner_limits is not None:
            try:
                launch.runner_limits.apply_to_process(proc.pid)
            except OSError as exc:
                msg = f"stage {stage_id}: rlimit setup failed: {exc}"
                raise WorkflowError(msg) from exc

        run = _StageRun(
            plan=plan,
            result=ResultSink(plan.label, plan.contract.result),
            stdout_tail=TailSink(self.TAIL_BYTES),
            stderr_tail=TailSink(self.TAIL_BYTES),
            wrap_out_tail=TailSink(self.TAIL_BYTES),
            wrap_err_tail=TailSink(self.TAIL_BYTES),
            proc=proc,
            group_id="",
        )
        runs[stage_id] = run

        sinks = self._stage_sinks(run, pipes_by_src, taps)

        feeds = self._stage_feeds(plan, launch.wrap_feeds, channels)

        pump.add(
            stage_id,
            proc=proc,
            channels=channels,
            sinks=sinks,
            feeds=feeds,
            timeout_sec=float(plan.rendered.timeout_sec),
            on_timeout=proc.kill,
        )

    @staticmethod
    def _stdin_fd(channels: SandboxChannels, edge: _EdgeLine | None) -> int:
        """Вход стадии: read-конец ребра, литеральный канал либо DEVNULL."""
        if edge is not None:
            return edge.read_fd

        if channels.has(Channel.TOOL_STDIN):
            return channels.child_fd(Channel.TOOL_STDIN)

        return subprocess.DEVNULL

    @staticmethod
    def _member_stdin_fd(
        channels: SandboxChannels,
        edge: _EdgeLine | None,
    ) -> int | None:
        """То же для стадии группы: None — супервизор подставит DEVNULL."""
        if edge is not None:
            return edge.read_fd

        if channels.has(Channel.TOOL_STDIN):
            return channels.child_fd(Channel.TOOL_STDIN)

        return None

    def _launch_group(  # noqa: PLR0913
        self,
        group: MountGroupPlan,
        plans: Mapping[str, StagePlan],
        edge_in: Mapping[str, _EdgeLine],
        pipes_by_src: Mapping[str, Sequence[PipeSink]],
        taps: _Taps,
        pump: ChannelPump,
        stack: ExitStack,
        cancellation: TurnCancellation,
        runs: dict[str, _StageRun],
        group_runs: dict[str, _GroupRun],
    ) -> None:
        """Mount-группа: один внешний процесс, N вложенных стадий супервизора."""
        group_channels = stack.enter_context(SandboxChannels(self.GROUP_CHANNELS))

        pass_fds: list[int] = list(group_channels.child_fds())
        entries: list[StageEntry] = []
        boots: list[_MemberBoot] = []
        cgroup_leaves: list[str] = []

        for stage_id in group.stage_ids:
            boot, entry = self._member_boot(
                plans[stage_id], edge_in.get(stage_id), pipes_by_src,
                taps, stack, runs, group,
            )
            boots.append(boot)
            entries.append(entry)

            pass_fds.extend(entry.owned_fds())

            if entry.cgroup_dir is not None:
                cgroup_leaves.append(entry.cgroup_dir)

        op: list[str] = [LauncherMode.GROUP.value]
        for entry in entries:
            op.append(entry.render())

        # wrap_args вычитывает --args внешнего bwrap: вниз канал не передаётся
        channel_env = dict(group_channels.child_env())
        channel_env.pop(Channel.WRAP_ARGS.env_name, None)

        # делегированные cgroup-leaf'ы стадий бинд-монтируются rw во внешнюю
        # песочницу: вход в них делает preexec супервизора
        rw_paths = list(group.rw_paths)
        rw_paths.extend(cgroup_leaves)

        try:
            require_fuse()
            chain = ChainChannelArgv.build(
                images=group.images,
                template=group.template,
                op=op,
                python_bin=sys.executable,
                options=group.options,
                limits=group.envelope,
                wrap_args_fd=group_channels.child_fd(Channel.WRAP_ARGS),
                rw_paths=rw_paths,
                network=group.network,
                channel_env=channel_env,
            )
        except MountError as exc:
            msg = f"group {group.group_id}: chain is not runnable: {exc}"
            raise WorkflowError(msg) from exc

        proc = self._spawn(
            chain.argv,
            stdin_fd=subprocess.DEVNULL,
            stdout_fd=group_channels.child_fd(Channel.WRAP_STDOUT),
            stderr_fd=group_channels.child_fd(Channel.WRAP_STDERR),
            pass_fds=tuple(dict.fromkeys(pass_fds)),
            cgroup_dir=None,
        )
        stack.callback(ChannelPump.reap, proc)
        stack.enter_context(cancellation.abort_with(proc.kill))

        group_channels.close_child_side()
        for boot in boots:
            boot.channels.close_child_side()

        watch = _GroupResultWatch(
            WrapResultSink(group.group_id),
            self._group_exit_hook(group, proc, pipes_by_src),
        )

        group_run = _GroupRun(
            plan=group,
            wrap_out_tail=TailSink(self.TAIL_BYTES),
            wrap_err_tail=TailSink(self.TAIL_BYTES),
            watch=watch,
            proc=proc,
        )
        group_runs[group.group_id] = group_run

        wrap_sinks: dict[Channel, ChannelSink] = {
            Channel.WRAP_STDOUT: group_run.wrap_out_tail,
            Channel.WRAP_STDERR: group_run.wrap_err_tail,
            Channel.WRAP_RESULT: watch,
        }

        group_timeout = 0.0
        for boot in boots:
            group_timeout = max(group_timeout, boot.timeout_sec)

        pump.add(
            group.group_id,
            proc=proc,
            channels=group_channels,
            sinks=wrap_sinks,
            feeds={Channel.WRAP_ARGS: WrapArgsCodec.encode(chain.bwrap_options)},
            timeout_sec=group_timeout,
            on_timeout=proc.kill,
        )

        for boot in boots:
            pump.add(
                boot.stage_id,
                proc=proc,
                channels=boot.channels,
                sinks=boot.sinks,
                feeds=boot.feeds,
                timeout_sec=boot.timeout_sec,
                on_timeout=proc.kill,
            )

    def _member_boot(  # noqa: PLR0913
        self,
        plan: StagePlan,
        edge: _EdgeLine | None,
        pipes_by_src: Mapping[str, Sequence[PipeSink]],
        taps: _Taps,
        stack: ExitStack,
        runs: dict[str, _StageRun],
        group: MountGroupPlan,
    ) -> tuple[_MemberBoot, StageEntry]:
        """Каналы, вложенный argv и cgroup одной стадии группы."""
        stage_id = plan.spec.id
        edge_fed = edge is not None

        wanted = self._stage_channels(plan, member=True, edge_fed=edge_fed)
        channels = stack.enter_context(SandboxChannels(wanted))

        channel_env = dict(channels.child_env())
        if edge_fed:
            channel_env[Channel.TOOL_STDIN.env_name] = str(
                SandboxChannels.STDIN_CHILD_FD
            )

        env = self._payload_env(plan.rendered) | channel_env

        inner = self._inner_profile(plan.rendered)
        command = shlex.join(plan.definition.entry)
        built = ChannelArgv.build(
            inner,
            command,
            env=env,
            wrap_args_fd=channels.child_fd(Channel.WRAP_ARGS_INNER),
            redirect_prefix=channels.redirect_prefix(),
            nested=True,
        )

        cgroup_dir = self._acquire_cgroup(plan, stack)

        entry = StageEntry(
            stage=stage_id,
            command=shlex.join(built.argv),
            stdin_fd=self._member_stdin_fd(channels, edge),
            pass_fds=channels.child_fds(),
            limits=plan.limits,
            cgroup_dir=cgroup_dir,
        )

        run = _StageRun(
            plan=plan,
            result=ResultSink(plan.label, plan.contract.result),
            stdout_tail=TailSink(self.TAIL_BYTES),
            stderr_tail=TailSink(self.TAIL_BYTES),
            wrap_out_tail=None,
            wrap_err_tail=None,
            proc=None,
            group_id=group.group_id,
        )
        runs[stage_id] = run

        sinks = self._stage_sinks(run, pipes_by_src, taps)

        feeds: dict[Channel, bytes] = {Channel.WRAP_ARGS_INNER: built.wrap_args}
        feeds[Channel.TOOL_ARGS] = plan.request_json.encode(self.ENCODING)
        if channels.has(Channel.TOOL_STDIN):
            feeds[Channel.TOOL_STDIN] = plan.literal

        boot = _MemberBoot(
            stage_id=stage_id,
            channels=channels,
            sinks=sinks,
            feeds=feeds,
            timeout_sec=float(plan.rendered.timeout_sec),
        )

        return boot, entry

    def _group_exit_hook(
        self,
        group: MountGroupPlan,
        proc: subprocess.Popen[bytes],
        pipes_by_src: Mapping[str, Sequence[PipeSink]],
    ) -> Callable[[str, int], None]:
        """Сбойный rc стадии группы убивает группу; каскад по графу делает насос."""

        def on_exit(stage: str, rc: int) -> None:
            if rc == 0:
                return

            if self._broken_sigpipe(rc, pipes_by_src.get(stage, ())):
                return

            logger.warning(
                "workflow: group %s stage %s exited rc=%s, killing the group",
                group.group_id,
                stage,
                rc,
            )
            proc.kill()

        return on_exit

    @staticmethod
    def _broken_sigpipe(rc: int, pipes: Sequence[PipeSink]) -> bool:
        """rc 141 при оборванном потребителем ребре: не собственный сбой."""
        if rc != ShellExit.SIGPIPE:
            return False

        broken = False
        for pipe in pipes:
            if pipe.broken:
                broken = True

        return broken

    def _launch_plan(
        self,
        plan: StagePlan,
        channels: SandboxChannels,
        env: Mapping[str, str],
        channel_env: Mapping[str, str],
    ) -> _LaunchPlan:
        """Argv обычной стадии: прямой bwrap либо fuse-цепочка одной стадии."""
        command = shlex.join(plan.definition.entry)

        if not plan.rendered.rw_images:
            built = ChannelArgv.build(
                plan.rendered,
                command,
                env=env,
                wrap_args_fd=channels.child_fd(Channel.WRAP_ARGS),
                redirect_prefix=channels.redirect_prefix(),
            )

            return _LaunchPlan(
                argv=built.argv,
                wrap_feeds={Channel.WRAP_ARGS: built.wrap_args},
                runner_limits=plan.limits,
            )

        inner = self._inner_profile(plan.rendered)
        built = ChannelArgv.build(
            inner,
            command,
            env=env,
            wrap_args_fd=channels.child_fd(Channel.WRAP_ARGS_INNER),
            redirect_prefix=channels.redirect_prefix(),
            nested=True,
        )

        images: list[tuple[str, str]] = []
        for bind in plan.rendered.rw_images:
            images.append(
                (ImageRef.canonical(bind.host), ImageRef.mount_point(bind.host))
            )

        rw_paths: list[str] = []
        for bind in plan.rendered.rw_binds:
            rw_paths.append(bind.host)

        # wrap_args вычитывает --args внешнего bwrap: вниз канал не передаётся
        outer_env = dict(channel_env)
        outer_env.pop(Channel.WRAP_ARGS.env_name, None)

        try:
            require_fuse()
            chain = ChainChannelArgv.build(
                images=images,
                template=plan.rendered.image_template,
                op=[LauncherMode.RUN.value, shlex.join(built.argv)],
                python_bin=sys.executable,
                options=plan.rendered.launcher.to_options(),
                limits=plan.limits,
                wrap_args_fd=channels.child_fd(Channel.WRAP_ARGS),
                rw_paths=rw_paths,
                network=plan.rendered.network,
                channel_env=outer_env,
            )
        except MountError as exc:
            msg = f"stage {plan.spec.id}: image chain is not runnable: {exc}"
            raise WorkflowError(msg) from exc

        wrap_feeds = {
            Channel.WRAP_ARGS: WrapArgsCodec.encode(chain.bwrap_options),
            Channel.WRAP_ARGS_INNER: built.wrap_args,
        }

        return _LaunchPlan(
            argv=chain.argv,
            wrap_feeds=wrap_feeds,
            runner_limits=None,
        )

    @staticmethod
    def _inner_profile(rendered: SandboxProfile) -> SandboxProfile:
        """Профиль вложенного bwrap: точки монтирования образов как rw-бинды."""
        mounts: list[BindSpec] = []
        for bind in rendered.rw_images:
            mounts.append(
                BindSpec(host=ImageRef.mount_point(bind.host), target=bind.target)
            )

        return rendered.model_copy(
            update={"rw_binds": rendered.rw_binds + tuple(mounts)}
        )

    def _acquire_cgroup(self, plan: StagePlan, stack: ExitStack) -> str | None:
        """Per-stage cgroup leaf; None — групповые лимиты не запрошены."""
        limits = GroupLimits.of_profile(plan.rendered)
        if not limits.requested:
            return None

        manager = CgroupManager(plan.rendered.cgroup_base)

        try:
            leaf = manager.acquire(limits)
        except CgroupError as exc:
            msg = f"stage {plan.spec.id}: cgroup acquire failed: {exc}"
            raise WorkflowError(msg) from exc

        stack.callback(manager.release, leaf)
        logger.info(
            "workflow[%s]: cgroup %s (%s)", plan.spec.id, leaf, limits.describe()
        )

        return leaf

    @staticmethod
    def _spawn(  # noqa: PLR0913
        argv: Sequence[str],
        *,
        stdin_fd: int,
        stdout_fd: int,
        stderr_fd: int,
        pass_fds: Sequence[int],
        cgroup_dir: str | None,
    ) -> subprocess.Popen[bytes]:
        preexec = WorkflowRunner.cgroup_preexec(cgroup_dir)

        try:
            return subprocess.Popen(  # noqa: S603
                list(argv),
                shell=False,
                stdin=stdin_fd,
                stdout=stdout_fd,
                stderr=stderr_fd,
                bufsize=0,
                close_fds=True,
                pass_fds=tuple(pass_fds),
                cwd="/",
                env=dict(os.environ),
                preexec_fn=preexec,  # noqa: PLW1509
            )
        except OSError as exc:
            raise WorkflowError(f"workflow spawn failed: {exc}") from exc

    @staticmethod
    def _payload_env(profile: SandboxProfile) -> dict[str, str]:
        """env профиля плюс уровень логов payload'а из логгера приложения."""
        env = dict(profile.env_set)

        level = logging.getLogger(WorkflowRunner.APP_LOGGER).getEffectiveLevel()
        env[PayloadLogging.LEVEL_ENV] = logging.getLevelName(level)

        return env

    def _stage_sinks(
        self,
        run: _StageRun,
        pipes_by_src: Mapping[str, Sequence[PipeSink]],
        taps: _Taps,
    ) -> dict[Channel, ChannelSink]:
        sinks: dict[Channel, ChannelSink] = {
            Channel.TOOL_STDOUT: self._stdout_sink(run, taps),
            Channel.TOOL_STDERR: RelaySink(run.plan.label, raw=run.stderr_tail),
            Channel.TOOL_RESULT: run.result,
        }

        if run.wrap_out_tail is not None:
            sinks[Channel.WRAP_STDOUT] = run.wrap_out_tail

        if run.wrap_err_tail is not None:
            sinks[Channel.WRAP_STDERR] = run.wrap_err_tail

        if run.plan.contract.out is None:
            return sinks

        payload = self._payload_sink(run.plan.spec.id, pipes_by_src, taps)
        if payload is not None:
            sinks[Channel.TOOL_PAYLOAD] = payload

        return sinks

    @staticmethod
    def _stdout_sink(run: _StageRun, taps: _Taps) -> ChannelSink:
        """Хвост tool_stdout; временно (этап 3) — ещё и tap живого потока."""
        tap = taps.stdout.get(run.plan.spec.id)
        if tap is None:
            return run.stdout_tail

        return FanoutSink((run.stdout_tail, tap))

    @staticmethod
    def _payload_sink(
        stage_id: str,
        pipes_by_src: Mapping[str, Sequence[PipeSink]],
        taps: _Taps,
    ) -> ChannelSink | None:
        """Веер рёбер и tap; None — потребителей нет, байты только считаются."""
        outs: list[ChannelSink] = []
        for pipe in pipes_by_src.get(stage_id, ()):
            outs.append(pipe)

        if tap := taps.payload.get(stage_id):
            outs.append(tap)

        if not outs:
            return None

        if len(outs) == 1:
            return outs[0]

        return FanoutSink(outs)

    def _stage_feeds(
        self,
        plan: StagePlan,
        wrap_feeds: Mapping[Channel, bytes],
        channels: SandboxChannels,
    ) -> dict[Channel, bytes]:
        """Подача входных каналов: профили обвязки, запрос и литеральный вход."""
        feeds: dict[Channel, bytes] = dict(wrap_feeds)

        feeds[Channel.TOOL_ARGS] = plan.request_json.encode(self.ENCODING)

        if channels.has(Channel.TOOL_STDIN):
            feeds[Channel.TOOL_STDIN] = plan.literal

        return feeds

    def _assemble(
        self,
        spec: WorkflowSpec,
        pump: ChannelPump,
        runs: Mapping[str, _StageRun],
        group_runs: Mapping[str, _GroupRun],
        pipes_by_src: Mapping[str, Sequence[PipeSink]],
    ) -> WorkflowOutcome:
        rc_map: dict[str, int] = {}
        reported: dict[str, bool] = {}
        for node in spec.nodes:
            rc, seen = self._final_rc(node.id, runs, group_runs)
            rc_map[node.id] = rc
            reported[node.id] = seen

        verdicts = self._verdicts(spec, rc_map, pipes_by_src)

        culprit = self._culprit(
            spec, pump, group_runs, verdicts, reported, runs, rc_map, pipes_by_src
        )
        if culprit is not None:
            self._raise_failure(culprit, pump, runs, group_runs, rc_map, reported)

        outcomes: list[StageOutcome] = []
        trailers: dict[str, JsonValue] = {}
        for node in spec.nodes:
            rc = rc_map[node.id]
            run = runs[node.id]
            outcomes.append(self._stage_outcome(node.id, rc, pump, run, group_runs))
            trailers[node.id] = self._stage_trailer(node.id, run, rc, pump)

        return WorkflowOutcome(
            stages=tuple(outcomes),
            trailers=trailers,
            journals=(),
        )

    @staticmethod
    def _final_rc(
        stage_id: str,
        runs: Mapping[str, _StageRun],
        group_runs: Mapping[str, _GroupRun],
    ) -> tuple[int, bool]:
        """Код стадии и признак «код действительно получен» (не подставлен)."""
        run = runs[stage_id]

        if run.proc is not None:
            return ShellExit.of(run.proc.returncode), True

        watch = group_runs[run.group_id].watch

        rc = watch.exits().get(stage_id)
        if rc is None:
            # группа умерла раньше отчёта: стадия убита вместе с ней
            return ShellExit.KILLED, False

        return rc, True

    def _verdicts(
        self,
        spec: WorkflowSpec,
        rc_map: Mapping[str, int],
        pipes_by_src: Mapping[str, Sequence[PipeSink]],
    ) -> dict[str, bool]:
        """Успех стадий с правилом rc 141; вердикты потребителей — раньше источников."""
        ok: dict[str, bool] = {}

        for stage_id in reversed(spec.order()):
            ok[stage_id] = self._stage_ok(
                spec, stage_id, rc_map[stage_id], pipes_by_src, ok
            )

        return ok

    @classmethod
    def _stage_ok(
        cls,
        spec: WorkflowSpec,
        stage_id: str,
        rc: int,
        pipes_by_src: Mapping[str, Sequence[PipeSink]],
        ok: Mapping[str, bool],
    ) -> bool:
        if rc == 0:
            return True

        if not cls._broken_sigpipe(rc, pipes_by_src.get(stage_id, ())):
            return False

        consumers_ok: list[bool] = []
        for consumer in spec.consumers_of(stage_id):
            consumers_ok.append(ok.get(consumer, False))

        return all(consumers_ok)

    def _culprit(  # noqa: PLR0913
        self,
        spec: WorkflowSpec,
        pump: ChannelPump,
        group_runs: Mapping[str, _GroupRun],
        verdicts: Mapping[str, bool],
        reported: Mapping[str, bool],
        runs: Mapping[str, _StageRun],
        rc_map: Mapping[str, int],
        pipes_by_src: Mapping[str, Sequence[PipeSink]],
    ) -> str | None:
        """Виновник: таймаут -> собственный сбойный rc -> обвязка группы."""
        if timed := self._timed_culprit(spec, pump, group_runs):
            return timed

        own = self._own_failure(
            spec, pump, verdicts, reported, runs, rc_map, pipes_by_src
        )
        if own is not None:
            return own

        for group_id, group_run in group_runs.items():
            if ShellExit.of(group_run.proc.returncode) != 0:
                return group_id

        for stage_id in spec.order():
            if not verdicts[stage_id]:
                return stage_id

        return None

    @staticmethod
    def _timed_culprit(
        spec: WorkflowSpec,
        pump: ChannelPump,
        group_runs: Mapping[str, _GroupRun],
    ) -> str | None:
        for stage_id in spec.order():
            if pump.timed_out(stage_id):
                return stage_id

        for group_id in group_runs:
            if pump.timed_out(group_id):
                return group_id

        return None

    @classmethod
    def _own_failure(  # noqa: PLR0913
        cls,
        spec: WorkflowSpec,
        pump: ChannelPump,
        verdicts: Mapping[str, bool],
        reported: Mapping[str, bool],
        runs: Mapping[str, _StageRun],
        rc_map: Mapping[str, int],
        pipes_by_src: Mapping[str, Sequence[PipeSink]],
    ) -> str | None:
        """Первая стадия, чей сбой — её собственный, а не каскад или смерть группы."""
        for stage_id in spec.order():
            if verdicts[stage_id]:
                continue
            if pump.kill_cause(stage_id) is KillCause.CASCADE:
                continue
            if runs[stage_id].proc is None and not reported[stage_id]:
                # жертва смерти группы, не собственный сбой
                continue
            if cls._broken_sigpipe(rc_map[stage_id], pipes_by_src.get(stage_id, ())):
                # законный 141: вердикт сломан сбоем потребителя, виновник — он
                continue
            return stage_id

        return None

    def _raise_failure(  # noqa: PLR0913
        self,
        culprit: str,
        pump: ChannelPump,
        runs: Mapping[str, _StageRun],
        group_runs: Mapping[str, _GroupRun],
        rc_map: Mapping[str, int],
        reported: Mapping[str, bool],
    ) -> None:
        if culprit in group_runs:
            self._raise_group_failure(culprit, pump, group_runs)

        run = runs[culprit]
        rc = rc_map[culprit]
        timed = pump.timed_out(culprit)

        stderr_text = self._failure_tails(run, group_runs)

        diagnostic = self._diagnostic(run, group_runs, rc=rc, timed_out=timed)

        tail = self._tail(stderr_text)
        context = f"stderr={tail!r}"
        if diagnostic:
            context = f"{context}; {diagnostic}"

        facts = FailureFacts(exit_code=rc, timed_out=timed, stderr_tail=stderr_text)
        self._log_failure(culprit, facts, pump.duration_ms(culprit), diagnostic)

        if timed:
            raise WorkflowError(f"workflow stage {culprit} timed out; {context}")

        # конверт ошибки объясняет отказ сам: он важнее ненулевого кода возврата
        failure = self._declared_failure(run.result, rc)
        if failure is not None:
            message = failure.message
            if diagnostic:
                message = f"{message}; {diagnostic}"
            raise PayloadFailureError(failure.kind, message)

        if run.proc is None and not reported[culprit]:
            msg = (
                f"workflow stage {culprit}: mount group finished without "
                f"its rc report; {context}"
            )
            raise WorkflowError(msg)

        wrap_tail = self._joined(self._wrap_tails(run, group_runs))
        if self._mount_failed(rc, wrap_tail):
            msg = (
                f"workflow stage {culprit}: image not mounted; "
                f"wrap_stderr={self._tail(wrap_tail)!r}"
            )
            raise WorkflowError(msg)

        raise WorkflowError(
            f"workflow stage {culprit} exited with code {rc}; {context}"
        )

    @staticmethod
    def _mount_failed(rc: int, wrap_tail: str) -> bool:
        """Сбой монтирования: код лаунчера плюс его собственный канал ошибок.

        В канальном режиме маркеров в stderr нет — wrap_stderr принадлежит
        обвязке целиком, поэтому непустой хвост и есть её жалоба.
        """
        if rc != LauncherExit.MOUNT_ERROR:
            return False

        return bool(wrap_tail)

    def _raise_group_failure(
        self,
        group_id: str,
        pump: ChannelPump,
        group_runs: Mapping[str, _GroupRun],
    ) -> None:
        group_run = group_runs[group_id]

        rc = ShellExit.of(group_run.proc.returncode)

        tail = self._tail(group_run.wrap_err_tail.text())

        if pump.timed_out(group_id):
            raise WorkflowError(
                f"workflow group {group_id} timed out; wrap_stderr={tail!r}"
            )

        if self._mount_failed(rc, tail):
            raise WorkflowError(
                f"workflow group {group_id}: image not mounted; wrap_stderr={tail!r}"
            )

        raise WorkflowError(
            f"workflow group {group_id} wrapper failed with code {rc}; "
            f"wrap_stderr={tail!r}"
        )

    def _failure_tails(
        self,
        run: _StageRun,
        group_runs: Mapping[str, _GroupRun],
    ) -> str:
        """Хвосты stderr стадии и её wrap-каналов (у члена группы — группы)."""
        tails: list[TailSink] = [run.stderr_tail]

        tails.extend(self._wrap_tails(run, group_runs))

        return self._joined(tails)

    @staticmethod
    def _wrap_tails(
        run: _StageRun,
        group_runs: Mapping[str, _GroupRun],
    ) -> list[TailSink]:
        """Хвосты wrap_stderr стадии и её группы: там говорит только обвязка."""
        tails: list[TailSink] = []

        if run.wrap_err_tail is not None:
            tails.append(run.wrap_err_tail)

        if run.group_id and run.group_id in group_runs:
            tails.append(group_runs[run.group_id].wrap_err_tail)

        return tails

    @staticmethod
    def _joined(tails: Sequence[TailSink]) -> str:
        parts: list[str] = []
        for tail in tails:
            text = tail.text().strip()
            if text:
                parts.append(text)

        return "\n".join(parts)

    @staticmethod
    def _declared_failure(
        sink: ResultSink[BaseModel],
        exit_code: int,
    ) -> ResultError | None:
        """Ожидаемый отказ из конверта; битый конверт упавшего процесса — не отказ."""
        if not sink.received:
            return None

        try:
            return sink.failure()
        except LauncherError:
            if exit_code == 0:
                raise
            # процесс упал посреди конверта: причину объяснят rc и stderr
            return None

    def _stage_outcome(
        self,
        stage_id: str,
        rc: int,
        pump: ChannelPump,
        run: _StageRun,
        group_runs: Mapping[str, _GroupRun],
    ) -> StageOutcome:
        killed = pump.kill_cause(stage_id) is KillCause.CASCADE
        timed = pump.timed_out(stage_id)

        diagnostic = ""
        if not killed:
            diagnostic = self._diagnostic(run, group_runs, rc=rc, timed_out=timed)

        return StageOutcome(
            stage=stage_id,
            exit_code=rc,
            duration_ms=pump.duration_ms(stage_id),
            timed_out=timed,
            killed_by_runner=killed,
            diagnostic=diagnostic,
        )

    def _diagnostic(
        self,
        run: _StageRun,
        group_runs: Mapping[str, _GroupRun],
        *,
        rc: int,
        timed_out: bool,
    ) -> str:
        """Объяснение лимитов сбойной стадии; убитой каскадом не применяется."""
        if rc == 0 and not timed_out:
            return ""

        facts = FailureFacts(
            exit_code=rc,
            timed_out=timed_out,
            stderr_tail=self._failure_tails(run, group_runs),
        )

        return SandboxDiagnostics.explain(facts, run.plan.rendered)

    def _stage_trailer(
        self,
        stage_id: str,
        run: _StageRun,
        rc: int,
        pump: ChannelPump,
    ) -> JsonValue:
        """Сырой data из конверта успеха; сверка bytes_out вне SIGPIPE-исхода."""
        if not run.result.received:
            raise WorkflowError(f"stage {stage_id}: no envelope in tool_result")

        try:
            envelope = run.result.success()
        except LauncherError as exc:
            raise WorkflowError(str(exc)) from exc

        # вердикт ok при rc != 0 — только SIGPIPE-исход: работа оборвана на
        # середине, квитанция вырожденная, поэтому ни схема, ни байты не сверяются
        if rc != 0:
            return envelope.data

        try:
            run.result.data()
        except LauncherError as exc:
            raise WorkflowError(str(exc)) from exc

        moved = pump.bytes_of(stage_id, Channel.TOOL_PAYLOAD)
        if envelope.bytes_out != moved:
            msg = (
                f"stage {stage_id}: bytes_out mismatch: envelope says "
                f"{envelope.bytes_out}, pump moved {moved}"
            )
            raise WorkflowError(msg)

        return envelope.data

    @classmethod
    def _log_failure(
        cls,
        stage: str,
        facts: FailureFacts,
        duration_ms: int,
        diagnostic: str,
    ) -> None:
        """Причина падения в лог: без неё код возврата не говорит ничего."""
        if facts.timed_out:
            reason = f"timed out after {duration_ms}ms"
        else:
            reason = f"rc={facts.exit_code}"

        tail = cls._tail(facts.stderr_tail)
        if not tail:
            tail = "<no output>"

        if diagnostic:
            tail = f"{tail}; {diagnostic}"

        logger.warning(
            "workflow stage %s: failed (%s); output tail: %s", stage, reason, tail
        )

    @classmethod
    def _tail(cls, text: str) -> str:
        stripped = text.strip()
        if len(stripped) <= cls.FAIL_TAIL_CHARS:
            return stripped

        return "…" + stripped[-cls.FAIL_TAIL_CHARS :]
