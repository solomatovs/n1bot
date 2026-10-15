"""Прогон acp-tck — Test Compatibility Kit протокола ACP
(https://github.com/agentclientprotocol/acp-tck) с `--protocol-version 2` —
против процесса `boba acp` (pytest -m integration). TCK сам запускает агента
отдельным процессом на каждый тест и проверяет рукопожатие, жизненный цикл
сессии, ход и отмену, гигиену транспорта и JSON-RPC; итог — JSON-отчёт с
вердиктом. Стенд: FakeLlmApp с медленной выдачей, который отвечает на вопрос
без маркера сценарием `answer`; ход держится, пока TCK шлёт `session/cancel`
и `session/close`.

Условие запуска: `--acp-tck <путь к acp-tck>`; TCK требует Python 3.14 и
ставится в своё окружение (`python3.14 -m venv …; pip install <checkout>`).
Без опции тест пропускается."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel, ConfigDict
from test_cli_acp import AcpProcess, FakeModel

from boba.stand.ui.fake_llm import ScenarioName

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

RUN_CEILING_SEC = 900.0


class TckTier(StrEnum):
    """Ярусы требований TCK."""

    MANDATORY = "MANDATORY"
    CAPABILITY = "CAPABILITY"
    ADVISORY = "ADVISORY"
    INFORMATIONAL = "INFORMATIONAL"


class TckStatus(StrEnum):
    """Статусы требований в отчёте TCK."""

    PASS = "PASS"
    FAIL = "FAIL"
    SKIPPED = "SKIPPED"
    NOT_TESTED = "NOT_TESTED"


class TckOutcome(BaseModel):
    """Итог одного теста TCK, привязанного к требованию."""

    model_config = ConfigDict(frozen=True)

    nodeid: str
    status: TckStatus
    message: str


class TckRequirement(BaseModel):
    """Требование отчёта: статус — худший по привязанным тестам."""

    model_config = ConfigDict(frozen=True)

    id: str
    tier: TckTier
    capability: str | None = None
    status: TckStatus
    tests: Sequence[TckOutcome] = ()


class TckVerdict(BaseModel):
    """Вердикт отчёта."""

    model_config = ConfigDict(frozen=True)

    conformant: bool
    blocked_by_auth: bool
    blocked_by_version_mismatch: bool
    tier_counts: Mapping[str, Mapping[str, int]]


class TckReport(BaseModel):
    """JSON-отчёт `--report-json` acp-tck."""

    model_config = ConfigDict(frozen=True)

    tck_version: str
    protocol_version: int
    schema_revision: str
    agent_info: Mapping[str, Any]
    requirements: Sequence[TckRequirement]
    verdict: TckVerdict

    def with_status(self, status: TckStatus) -> list[TckRequirement]:
        found: list[TckRequirement] = []
        for requirement in self.requirements:
            if requirement.status is status:
                found.append(requirement)

        return found

    def by_id(self, requirement_id: str) -> TckRequirement:
        for requirement in self.requirements:
            if requirement.id == requirement_id:
                return requirement

        msg = f"requirement {requirement_id} is not in the TCK report"
        raise AssertionError(msg)


class TckStand:
    """Каталог прогона: файл настроек 0600 над фейковой моделью, рабочий
    каталог агента, команды агента и TCK."""

    AGENT_MODULE = "boba.cli.app"
    SETTING_SOURCES = "user"
    PROTOCOL_VERSION = "2"

    def __init__(self, root: Path, model: FakeModel) -> None:
        self._root = root
        self._process = AcpProcess(root, model)

    def prepare(self) -> None:
        self._process.write_settings(self._process.settings_document())
        self.agent_cwd().mkdir(exist_ok=True)
        self.home().mkdir(exist_ok=True)

    def agent_cwd(self) -> Path:
        return self._root / "cwd"

    def home(self) -> Path:
        return self._root / "home"

    def report_path(self) -> Path:
        return self._root / "report.json"

    def agent_command(self) -> list[str]:
        return [
            sys.executable,
            "-m",
            self.AGENT_MODULE,
            "acp",
            "--settings",
            str(self._process.settings_path()),
            "--setting-sources",
            self.SETTING_SOURCES,
        ]

    def tck_command(self, executable: Path) -> list[str]:
        return [
            str(executable),
            "--protocol-version",
            self.PROTOCOL_VERSION,
            "--agent-cwd",
            str(self.agent_cwd()),
            "--timeout",
            "30",
            "--test-timeout",
            "120",
            "--cancel-prompt",
            ScenarioName.ANSWER.value,
            "--report-json",
            str(self.report_path()),
            "-v",
            "--",
            *self.agent_command(),
        ]

    def environment(self) -> dict[str, str]:
        return {"HOME": str(self.home()), "PATH": os.environ["PATH"]}

    def report(self) -> TckReport:
        raw = json.loads(self.report_path().read_text(encoding="utf-8"))

        return TckReport.model_validate(raw)


class TckRun:
    """Запуск acp-tck субпроцессом: вывод копится и печатается целиком."""

    def __init__(self, stand: TckStand, executable: Path) -> None:
        self._stand = stand
        self._executable = executable
        self.output = ""

    async def run(self) -> int:
        process = await asyncio.create_subprocess_exec(
            *self._stand.tck_command(self._executable),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=self._stand.environment(),
            cwd=str(self._stand.agent_cwd()),
        )
        assert process.stdout is not None
        try:
            raw = await asyncio.wait_for(process.stdout.read(), RUN_CEILING_SEC)
        finally:
            if process.returncode is None:
                process.kill()

        self.output = raw.decode("utf-8", errors="replace")

        return await process.wait()


class TestTck:
    WITHOUT_AUTH: ClassVar[frozenset[str]] = frozenset({"ACP-AUTH-206", "ACP-AUTH-207"})
    """Требования входа, которые агент без authMethods пропускает по праву."""

    async def test_v2_suite_is_conformant(
        self, tck_executable: Path, answering_model: FakeModel, tmp_path: Path
    ) -> None:
        stand = TckStand(tmp_path, answering_model)
        stand.prepare()
        run = TckRun(stand, tck_executable)
        code = await run.run()
        print(run.output)

        assert stand.report_path().is_file(), run.output[-4000:]
        report = stand.report()
        print(
            f"acp-tck {report.tck_version}, schema {report.schema_revision}, "
            f"agent {report.agent_info}, counts {dict(report.verdict.tier_counts)}"
        )

        assert code == 0, run.output[-4000:]
        assert report.protocol_version == 2
        assert report.verdict.conformant
        assert not report.verdict.blocked_by_auth
        assert not report.verdict.blocked_by_version_mismatch
        assert report.with_status(TckStatus.FAIL) == []
        assert report.with_status(TckStatus.NOT_TESTED) == []

        for requirement in report.with_status(TckStatus.SKIPPED):
            print(f"skipped {requirement.id}: {requirement.tests[0].message}")
            if requirement.id in self.WITHOUT_AUTH:
                continue

            assert requirement.tier is TckTier.CAPABILITY, requirement

        for exercised in ("ACP-CANCEL-201", "ACP-CANCEL-202", "ACP-CLOSE-202"):
            assert report.by_id(exercised).status is TckStatus.PASS, exercised
