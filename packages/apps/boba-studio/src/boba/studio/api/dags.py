"""REST-запуск DAG потоковых вызовов человеком: POST /v1/dags/runs.

Тело — профиль, intent и описание DAG (DagSpec): узлы — вызовы инструментов
с полями каналов, как их делает модель. Инструменты — видимые субъекту вне
чата (ToolRegistry.for_headless), исполнитель — DagRunner, тот же, что у
чата; контекст вызова собирается под HumanInitiator(api) в своей области
job. Ответ — итог каждого узла; срыв группы — не ошибка HTTP, а итог узлов.

Ошибки (HTTP):
401 — вход не сохранён слоем данных.
403 — профиль недоступен ролям пользователя.
404 — узел зовёт инструмент, недоступный субъекту вне чата.
409 — область вызова занята.
422 — план каналов DAG нарушен: поля портов, писатели, читатели, цикл.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, ClassVar
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from boba.chat.profiles import ChatProfiles
from boba.identity.context import Scope
from boba.identity.run import Runs
from boba.studio.api.auth import ApiAuth, CurrentUser
from boba.studio.api.tools import JobLock, LocksSource, RegistrySource
from boba.studio.api.urls import DagUrl
from boba.toolkit.calls import ToolIntent
from boba.toolkit.chain import StreamPlanError
from boba.toolkit.dag import DagNode, DagSpec, NodeOutcome
from boba.toolrun.dag_run import (
    DagHandle,
    DagOutcome,
    DagRunError,
    DagRunner,
)
from boba.toolrun.hosted import DirectCalls

__all__ = ["DagNodeReply", "DagRunBody", "DagRunReply", "DagRunning"]

logger = logging.getLogger(__name__)


class DagRunBody(BaseModel):
    """Тело запроса: от какого профиля и с каким намерением запускать DAG."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile: str = Field(min_length=1)
    intent: str = Field(min_length=1, max_length=ToolIntent.MAX_CHARS)
    dag: DagSpec


class DagNodeReply(BaseModel):
    """Итог узла: текст для читателя и результат инструмента как есть."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    tool: str
    ok: bool
    content: str
    result: Mapping[str, Any]

    @classmethod
    def of(cls, outcome: NodeOutcome) -> DagNodeReply:
        return cls(
            key=outcome.key,
            tool=outcome.tool,
            ok=not outcome.failed(),
            content=outcome.content,
            result=outcome.artifact.model_dump(mode="json"),
        )


class DagRunReply(BaseModel):
    """Итог запуска DAG: область запуска и итоги узлов в порядке описания."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    dag: str
    version: int
    ok: bool
    nodes: Sequence[DagNodeReply]

    @classmethod
    def of(cls, run_id: str, outcome: DagOutcome) -> DagRunReply:
        nodes: list[DagNodeReply] = []
        for node in outcome.nodes:
            nodes.append(DagNodeReply.of(node))

        return cls(
            run_id=run_id,
            dag=outcome.dag,
            version=outcome.version,
            ok=outcome.ok(),
            nodes=nodes,
        )


class DagRunning:
    """Обработчик POST /dags/runs."""

    TAG: ClassVar[str] = "dags"

    def __init__(
        self,
        registry: RegistrySource,
        profiles: ChatProfiles,
        locks: LocksSource,
        heartbeat_sec: float,
        runs: Runs,
    ) -> None:
        self._registry = registry
        self._profiles = profiles
        self._job_lock = JobLock(locks, heartbeat_sec)
        self._runs = runs
        self._calls = DirectCalls()

    def mount(self, router: APIRouter) -> None:
        router.add_api_route(
            DagUrl.RUNS.value, self.serve, methods=["POST"], tags=[self.TAG]
        )

    async def serve(self, body: DagRunBody, current_user: CurrentUser) -> DagRunReply:
        identity = ApiAuth.resolve(current_user, body.profile, self._profiles)

        job_id = str(uuid4())
        context = identity.context(Scope.job(job_id))

        registry = await self._registry()
        tools = registry.for_headless(identity.subject.roles, identity.subject.profile)
        runner = registry.runner(tools, self._calls)
        dag = self._with_intent(body.dag, body.intent)

        logger.info(
            "api dag run: %s v%d by %s in job %s",
            dag.name,
            dag.version,
            context.subject.login,
            job_id,
        )

        async with self._job_lock.held(context):
            with self._runs.open(context):
                handle = self._start(runner, dag)
                outcome = await handle.outcome()

        return DagRunReply.of(job_id, outcome)

    @staticmethod
    def _start(runner: DagRunner, dag: DagSpec) -> DagHandle:
        try:
            return runner.start(dag)
        except StreamPlanError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except DagRunError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    def _with_intent(self, dag: DagSpec, intent: str) -> DagSpec:
        """Намерение запроса — каждому узлу без своего, как у одиночного вызова."""
        nodes = list(self._intended(dag.nodes, intent))

        return DagSpec(name=dag.name, version=dag.version, nodes=nodes)

    @staticmethod
    def _intended(nodes: Sequence[DagNode], intent: str) -> Iterator[DagNode]:
        for node in nodes:
            if ToolIntent.NAME in node.args:
                yield node
                continue

            args: dict[str, Any] = dict(node.args)
            args[ToolIntent.NAME] = intent
            yield DagNode(key=node.key, tool=node.tool, args=args)
