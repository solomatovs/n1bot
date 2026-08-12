"""Локальный исполнитель узлов doc: контракт каналов тот же, песочницы нет.

Payload запускается обычным процессом на настоящих pipe-каналах (tool_args,
tool_payload, tool_result), поэтому проверяется весь путь узла — обогащение
args, модель запроса, поток данных и квитанция — без bwrap и rootfs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Mapping
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, ValidationError

from boba.toolkit.channels import (
    Channel,
    ChannelSink,
    ResultFailure,
    ResultSuccess,
    StreamKey,
    ValidationSummary,
)
from boba.toolkit.launcher import (
    ChannelHead,
    LauncherError,
    PayloadFailureError,
    ToolLauncher,
)
from boba.toolkit.workflow import (
    StageNode,
    StageOutcome,
    WorkflowError,
    WorkflowOutcome,
    WorkflowSpec,
)


class StageRun(BaseModel):
    """Итог локального прогона одной стадии: код, конверт квитанции, stderr."""

    model_config = ConfigDict(frozen=True)

    code: int
    envelope: dict[str, Any] | None
    stderr: str


class SinkPump(threading.Thread):
    """Насос канала данных: читает tool_payload по ходу стадии, как раннер."""

    CHUNK: ClassVar[int] = 65536

    def __init__(self, read_fd: int, sink: ChannelSink | None) -> None:
        super().__init__(daemon=True)
        self._read_fd = read_fd
        self._sink = sink
        self._failure: BaseException | None = None
        self.data = bytearray()

    def run(self) -> None:
        try:
            self._pump()
        except BaseException as exc:
            self._failure = exc

        os.close(self._read_fd)

    def collect(self) -> bytes:
        """Дождаться конца канала; сбой приёмника поднимается вызывающему."""
        self.join()

        if self._failure is not None:
            raise self._failure

        return bytes(self.data)

    def _pump(self) -> None:
        while True:
            piece = os.read(self._read_fd, self.CHUNK)
            if not piece:
                break

            self.data.extend(piece)

            if self._sink is not None:
                self._sink.feed(piece)

        if self._sink is not None:
            self._sink.close()


class LocalStageLauncher(ToolLauncher):
    """Порт запуска поверх локального процесса; узлы берутся из реестра пакета."""

    ENCODING: ClassVar[str] = "utf-8"

    TIMEOUT_SEC: ClassVar[int] = 120

    def __init__(self, nodes: Mapping[str, StageNode]) -> None:
        self._nodes = nodes
        self.requests: list[dict[str, Any]] = []
        self.payloads: list[bytes] = []

    def call(
        self,
        spec: WorkflowSpec,
        sinks: Mapping[str, ChannelSink] | None = None,
    ) -> WorkflowOutcome:
        trailers: dict[str, Any] = {}
        stages: list[StageOutcome] = []

        for node in spec.nodes:
            definition = self._nodes.get(node.tool)
            if definition is None:
                raise WorkflowError(f"unknown workflow tool: {node.tool}")

            request = self._request(definition, node.args)

            sink: ChannelSink | None = None
            if sinks is not None:
                sink = sinks.get(node.id)

            run = self._run(definition, request, sink)

            trailers[node.id] = self._data(run)
            stages.append(
                StageOutcome(
                    stage=node.id,
                    exit_code=run.code,
                    duration_ms=0,
                    timed_out=False,
                    killed_by_runner=False,
                    diagnostic="",
                )
            )

        return WorkflowOutcome(stages=stages, trailers=trailers)

    def head(self, key: StreamKey, max_bytes: int) -> ChannelHead:
        """Журнала у тестового исполнителя нет: голова канала пуста."""
        return ChannelHead.empty()

    def _request(self, definition: StageNode, args: Mapping[str, Any]) -> BaseModel:
        """Обогащение и валидация до запуска — как в WorkflowRunner._plan_stage."""
        enriched = definition.enrich(args)

        try:
            request = definition.request.model_validate(enriched)
        except ValidationError as exc:
            summary = ValidationSummary.of(exc)
            msg = f"args do not match the node request: {summary}"
            raise WorkflowError(msg) from exc

        self.requests.append(json.loads(request.model_dump_json()))

        return request

    @staticmethod
    def _data(run: StageRun) -> Any:
        if run.envelope is None:
            raise LauncherError(f"stage wrote no receipt: {run.stderr}")

        if "error" in run.envelope:
            failure = ResultFailure.model_validate(run.envelope)
            raise PayloadFailureError(failure.error.kind, failure.error.message)

        if run.code != 0:
            raise LauncherError(f"stage failed with {run.code}: {run.stderr}")

        return ResultSuccess.model_validate(run.envelope).data

    def _run(
        self,
        definition: StageNode,
        request: BaseModel,
        sink: ChannelSink | None,
    ) -> StageRun:
        args_r, args_w = os.pipe()
        result_r, result_w = os.pipe()
        payload_r, payload_w = os.pipe()

        env = dict(os.environ)
        env[Channel.TOOL_ARGS.env_name] = str(args_r)
        env[Channel.TOOL_RESULT.env_name] = str(result_w)
        env[Channel.TOOL_PAYLOAD.env_name] = str(payload_w)
        env[Channel.TOOL_STDOUT.env_name] = "1"
        env[Channel.TOOL_STDERR.env_name] = "2"

        argv = [sys.executable, *definition.entry[1:]]

        proc = subprocess.Popen(  # noqa: S603
            argv,
            pass_fds=(args_r, result_w, payload_w),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

        os.close(args_r)
        os.close(result_w)
        os.close(payload_w)

        os.write(args_w, request.model_dump_json().encode(self.ENCODING))
        os.close(args_w)

        pump = SinkPump(payload_r, sink)
        pump.start()

        _, stderr = proc.communicate(timeout=self.TIMEOUT_SEC)

        self.payloads.append(pump.collect())

        raw = self._read_all(result_r).decode(self.ENCODING).strip()

        envelope: dict[str, Any] | None = None
        if raw:
            envelope = json.loads(raw)

        return StageRun(
            code=proc.returncode,
            envelope=envelope,
            stderr=stderr.decode(self.ENCODING, errors="replace"),
        )

    @staticmethod
    def _read_all(fd: int) -> bytes:
        data = bytearray()
        while True:
            piece = os.read(fd, SinkPump.CHUNK)
            if not piece:
                break
            data.extend(piece)
        os.close(fd)

        return bytes(data)
