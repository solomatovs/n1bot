"""Прогон acp-tck — Test Compatibility Kit протокола ACP
(https://github.com/agentclientprotocol/acp-tck) с `--protocol-version 2` —
против процесса `boba --stdio` (pytest -m integration). TCK сам запускает агента
отдельным процессом на каждый тест и проверяет рукопожатие, жизненный цикл
сессии, ход и отмену, гигиену транспорта и JSON-RPC; итог — JSON-отчёт с
вердиктом. Набор идёт частями по модулям TCK одновременно, вердикт считается
по объединению отчётов частей. Стенд: FakeLlmApp, который отвечает на вопрос
без маркера сценарием `answer` без задержки, а на вопрос отмены — медленно
по книге ответов: ход держится, пока TCK шлёт `session/cancel` и
`session/close`.

Условие запуска: `--acp-tck <путь к acp-tck>`; TCK требует Python 3.14 и
ставится в своё окружение (`python3.14 -m venv …; pip install <checkout>`).
Без опции тест пропускается."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Coroutine, Iterator, Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel, ConfigDict
from test_cli_acp import AcpProcess, FakeModel

from boba.stand.ui.fake_llm import Reply, ReplyBookClient, ReplyTurn
from boba.stand.ui.stand import StandUrl

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


class TckTier(StrEnum):
    """Ярусы требований TCK."""

    MANDATORY = "MANDATORY"
    CAPABILITY = "CAPABILITY"
    ADVISORY = "ADVISORY"
    INFORMATIONAL = "INFORMATIONAL"


class TckStatus(StrEnum):
    """Статусы требований в отчёте TCK по старшинству при сводке."""

    FAIL = "FAIL"
    PASS = "PASS"
    SKIPPED = "SKIPPED"
    NOT_TESTED = "NOT_TESTED"

    def worse(self, other: TckStatus) -> TckStatus:
        """Худший из двух статусов требования, как его сводит TCK: FAIL,
        затем PASS, затем SKIPPED, затем NOT_TESTED."""
        order = list(TckStatus)
        if order.index(self) <= order.index(other):
            return self

        return other


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

    def nodeids(self) -> set[str]:
        """Тесты TCK, которые дали итог в этом прогоне."""
        found: set[str] = set()
        for requirement in self.requirements:
            for outcome in requirement.tests:
                found.add(outcome.nodeid)

        return found


class TckPart(StrEnum):
    """Часть набора TCK — выражение `-k` по модулям и тестам conformance.

    Части идут одновременно, каждая своим процессом acp-tck; длительность
    набора — длительность самой долгой части, поэтому части подобраны
    примерно равными. Последняя часть берёт всё, что не попало в остальные,
    поэтому объединение частей — весь набор TCK при любом его составе, в том
    числе после обновления TCK.
    """

    CONCURRENT_PROMPT = "test_concurrent_prompt_behaviour"
    """Отдельно: ACP-INFO-CONCURRENT-201 ждёт ответ на первый вопрос весь
    --timeout, потому что TCK читает его мимо уже принятых строк."""

    SESSION_LISTING = "test_session_capabilities.py and (list or delete or close)"
    SESSION_RESUME = "test_session_capabilities.py and not (list or delete or close)"
    CANCEL = "test_cancel.py"
    EXTENSIBILITY = "test_extensibility.py"
    INITIALIZE = "test_initialize.py or test_session.py"
    BATCH = "test_batch.py or test_diagnostics.py"
    PATCHES = "test_patches.py or test_enums.py"
    JSONRPC = "test_jsonrpc.py or test_transport.py"
    PROMPT = "test_prompt.py or test_permission.py or test_prompt_capabilities.py"
    SESSION_CONFIG = "test_session_config.py or test_client_capabilities.py"
    REST = "rest"

    def expression(self) -> str:
        if self is not TckPart.REST:
            return self.value

        named: list[str] = []
        for part in TckPart:
            if part is TckPart.REST:
                continue

            named.append(f"({part.value})")

        listed = " or ".join(named)
        return f"not ({listed})"


class TckMerge:
    """Сводит отчёты частей в отчёт всего набора по правилам TCK: статус
    требования — худший по его тестам из всех частей, NOT_TESTED — ни одного
    теста ни в одной части; вердикт — без FAIL и NOT_TESTED у MANDATORY, без
    FAIL у CAPABILITY и без блокировки входом или версией."""

    def merge(self, reports: Sequence[TckReport]) -> TckReport:
        first = reports[0]
        requirements: list[TckRequirement] = []
        for requirement in first.requirements:
            requirements.append(self._requirement(requirement, reports))

        verdict = TckVerdict(
            conformant=self._conformant(requirements, reports),
            blocked_by_auth=self._blocked_by_auth(reports),
            blocked_by_version_mismatch=self._blocked_by_version(reports),
            tier_counts=self._tier_counts(requirements),
        )

        merged = {"requirements": requirements, "verdict": verdict}
        return first.model_copy(update=merged)

    def _requirement(
        self, requirement: TckRequirement, reports: Sequence[TckReport]
    ) -> TckRequirement:
        tests: list[TckOutcome] = []
        for report in reports:
            tests.extend(report.by_id(requirement.id).tests)

        status = TckStatus.NOT_TESTED
        for outcome in tests:
            status = status.worse(outcome.status)

        return requirement.model_copy(update={"status": status, "tests": tests})

    def _conformant(
        self, requirements: Sequence[TckRequirement], reports: Sequence[TckReport]
    ) -> bool:
        if self._blocked_by_auth(reports):
            return False

        if self._blocked_by_version(reports):
            return False

        for requirement in requirements:
            if requirement.status is TckStatus.FAIL:
                if requirement.tier is TckTier.MANDATORY:
                    return False

                if requirement.tier is TckTier.CAPABILITY:
                    return False

            if requirement.status is TckStatus.NOT_TESTED:
                if requirement.tier is TckTier.MANDATORY:
                    return False

        return True

    def _blocked_by_auth(self, reports: Sequence[TckReport]) -> bool:
        blocked = self._auth_blocks(reports)
        return any(blocked)

    def _blocked_by_version(self, reports: Sequence[TckReport]) -> bool:
        blocked = self._version_blocks(reports)
        return any(blocked)

    def _auth_blocks(self, reports: Sequence[TckReport]) -> Iterator[bool]:
        for report in reports:
            yield report.verdict.blocked_by_auth

    def _version_blocks(self, reports: Sequence[TckReport]) -> Iterator[bool]:
        for report in reports:
            yield report.verdict.blocked_by_version_mismatch

    def _tier_counts(
        self, requirements: Sequence[TckRequirement]
    ) -> dict[str, dict[str, int]]:
        counts: dict[str, dict[str, int]] = {}
        for tier in TckTier:
            by_status: dict[str, int] = {}
            for status in TckStatus:
                by_status[status.value] = 0

            counts[tier.value] = by_status

        for requirement in requirements:
            counts[requirement.tier.value][requirement.status.value] += 1

        return counts


class TckStand:
    """Каталог прогона части: файл настроек 0600 над фейковой моделью,
    рабочий каталог и HOME агента, команды агента и TCK."""

    AGENT_MODULE = "boba.cli.app"
    SETTING_SOURCES = "user"
    PROTOCOL_VERSION = "2"
    RESPONSE_TIMEOUT_SEC = "20"
    """Срок ответа агента; quiet_period TCK — min(2, timeout / 10), так что
    ожидание тишины остаётся 2 с, а ACP-INFO-CONCURRENT-201 ждёт 20 с."""
    CANCEL_QUESTION: ClassVar[str] = "tck: a long answer to cancel"
    """Вопрос тестов отмены TCK: книга ответов отвечает на него медленно."""

    def __init__(self, root: Path, model: FakeModel, part: TckPart) -> None:
        self._root = root
        self._part = part
        self._process = AcpProcess(root, model)

    @property
    def part(self) -> TckPart:
        return self._part

    def prepare(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
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
            "--stdio",
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
            self.RESPONSE_TIMEOUT_SEC,
            "--test-timeout",
            "120",
            "--cancel-prompt",
            self.CANCEL_QUESTION,
            "--report-json",
            str(self.report_path()),
            "-k",
            self._part.expression(),
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
    """Запуск acp-tck части субпроцессом: вывод копится и печатается целиком."""

    CEILING_SEC: ClassVar[float] = 900.0

    def __init__(self, stand: TckStand, executable: Path) -> None:
        self.stand = stand
        self._executable = executable
        self.output = ""

    async def run(self) -> int:
        self.stand.prepare()
        process = await asyncio.create_subprocess_exec(
            *self.stand.tck_command(self._executable),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=self.stand.environment(),
            cwd=str(self.stand.agent_cwd()),
        )
        assert process.stdout is not None
        # kill опрашивает процесс (Popen.poll) и уводит статус от наблюдателя asyncio,
        # тот отдаёт код 255: убивается только часть, не уложившаяся в срок
        try:
            async with asyncio.timeout(self.CEILING_SEC):
                raw = await process.stdout.read()
                code = await process.wait()
        finally:
            if process.returncode is None:
                process.kill()

        self.output = raw.decode("utf-8", errors="replace")

        return code


class TckSuite:
    """Весь набор TCK: части (TckPart) одновременно над одной фейковой
    моделью, у каждой свой каталог, HOME и отчёт; итог — отчёт TckMerge.

    Книга ответов модели держит медленный ответ на вопрос отмены: ход идёт
    дольше, чем TCK шлёт `session/cancel`, а прочие вопросы TCK получают
    ответ сценарием без задержки.
    """

    CANCEL_TOKEN_DELAY_SEC: ClassVar[float] = 0.2
    CANCEL_WORDS: ClassVar[int] = 36
    OK_CODES: ClassVar[frozenset[int]] = frozenset(
        {pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED}
    )
    """Коды acp-tck, при которых часть дошла до отчёта: TESTS_FAILED значит
    лишь, что вердикт части без остальных частей неполон."""

    def __init__(self, root: Path, model: FakeModel, executable: Path) -> None:
        self._book = ReplyBookClient(StandUrl.of(model.port))
        self._merge = TckMerge()
        self.runs: list[TckRun] = []
        for part in TckPart:
            stand = TckStand(root / part.name.lower(), model, part)
            self.runs.append(TckRun(stand, executable))

    async def run(self) -> TckReport:
        await asyncio.to_thread(self._book.register, [self._cancel_reply()])

        pending: list[Coroutine[Any, Any, int]] = []
        for run in self.runs:
            pending.append(run.run())

        codes = await asyncio.gather(*pending)

        reports: list[TckReport] = []
        for run, code in zip(self.runs, codes, strict=True):
            print(run.output)
            part = run.stand.part.name
            tail = run.output[-4000:]
            assert run.stand.report_path().is_file(), f"part {part}: {tail}"
            assert code in self.OK_CODES, f"part {part} exited with {code}: {tail}"
            reports.append(run.stand.report())

        self._expect_disjoint(reports)

        return self._merge.merge(reports)

    def _cancel_reply(self) -> Reply:
        words = " ".join(["slowly"] * self.CANCEL_WORDS)
        turn = ReplyTurn(content=f"Here is a long streamed answer: {words}")

        return Reply(
            question=TckStand.CANCEL_QUESTION,
            turns=[turn],
            token_delay_sec=self.CANCEL_TOKEN_DELAY_SEC,
        )

    def _expect_disjoint(self, reports: Sequence[TckReport]) -> None:
        seen: dict[str, str] = {}
        for run, report in zip(self.runs, reports, strict=True):
            part = run.stand.part.name
            ran = report.nodeids()
            assert ran, f"part {part} ran no TCK test"

            for nodeid in ran:
                other = seen.get(nodeid)
                assert other is None, f"{nodeid} ran in parts {other} and {part}"
                seen[nodeid] = part


class TestTck:
    WITHOUT_AUTH: ClassVar[frozenset[str]] = frozenset({"ACP-AUTH-206", "ACP-AUTH-207"})
    """Обязательные требования входа, которые агент без authMethods пропускает
    по праву; прочие обязательные требования TCK обязан проверить."""

    async def test_v2_suite_is_conformant(
        self, tck_executable: Path, answering_model: FakeModel, tmp_path: Path
    ) -> None:
        suite = TckSuite(tmp_path, answering_model, tck_executable)
        report = await suite.run()
        print(
            f"acp-tck {report.tck_version}, schema {report.schema_revision}, "
            f"agent {report.agent_info}, counts {dict(report.verdict.tier_counts)}"
        )

        assert report.protocol_version == 2
        assert report.verdict.conformant
        assert not report.verdict.blocked_by_auth
        assert not report.verdict.blocked_by_version_mismatch
        assert report.with_status(TckStatus.FAIL) == []
        assert report.with_status(TckStatus.NOT_TESTED) == []

        for requirement in report.with_status(TckStatus.SKIPPED):
            print(f"skipped {requirement.id}: {requirement.tests[0].message}")
            if requirement.tier is not TckTier.MANDATORY:
                continue

            assert requirement.id in self.WITHOUT_AUTH, requirement

        for exercised in ("ACP-CANCEL-201", "ACP-CANCEL-202", "ACP-CLOSE-202"):
            assert report.by_id(exercised).status is TckStatus.PASS, exercised
