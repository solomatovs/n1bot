"""Собранный агент `release/<версия>/bin/boba.dist/boba.bin`: сценарий ACP через него —
рукопожатие, сессия с регистрацией MCP-over-ACP, ход с инструментом,
разрешение; история в Postgres стенда, как у чата в проде, — сессия, ход,
возобновление с воспроизведением и второй ход; самопроверка данных пакетов;
время старта и размер, отсутствие исходного текста внутри. Локальную модель
onnx проверяет `bin/boba-onnx.dist/boba.bin` — сборка с рантаймом onnxruntime-genai.

Тесты проверяют артефакт, а не код, и в обычный прогон не входят: маркер
binary — после `make -C build boba-bin`, binary_onnx — после
`make -C build boba-bin BIN_ONNX=1`. Каталог bin задаёт `--boba-bin`, без
него берётся release основного дерева: бинарник собирают только там. Без
файла тест падает с подсказкой сборки."""

from __future__ import annotations

import subprocess
import time
from enum import StrEnum
from importlib.metadata import version
from pathlib import Path
from typing import ClassVar

import pytest
from acp.experimental.v2 import schema
from omegaconf import DictConfig
from test_cli_acp import (
    AcpClient,
    AcpProcess,
    FakeModel,
    FeedView,
    StandRegistration,
    Steps,
    call,
    handshake,
)
from test_cli_acp_postgres import PostgresStand

from boba.db.postgres import AsyncPostgresPool
from boba.stand.ui.fake_llm import ScenarioName
from boba.transport.acp.protocol import StopReason

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


class ReleaseBinary(StrEnum):
    """Бинарники выпуска в каталоге bin и команды, которые их собирают."""

    BOBA = "boba.dist/boba.bin"
    ONNX = "boba-onnx.dist/boba.bin"

    @property
    def build(self) -> str:
        if self is ReleaseBinary.ONNX:
            return "make -C build boba-bin BIN_ONNX=1"

        return "make -C build boba-bin"


class ReleaseBin:
    """Каталог bin собранного выпуска для тестов артефакта.

    Создаётся фикстурой release_bin из опции `--boba-bin`; без опции — каталог
    release/<версия boba-cli>/bin основного дерева репозитория, которое
    называет git (общий каталог .git worktree'ов). path() отдаёт файл
    бинарника или валит тест с командой сборки.
    """

    REPO: ClassVar[Path] = Path(__file__).resolve().parents[4]

    def __init__(self, given: object) -> None:
        self._dir = self._directory(given)

    def path(self, binary: ReleaseBinary) -> Path:
        path = self._dir / binary.value
        if not path.is_file():
            pytest.fail(f"no binary at {path}; run: {binary.build}")

        return path

    def _directory(self, given: object) -> Path:
        if given is not None:
            return Path(str(given))

        command = [
            "git",
            "-C",
            str(self.REPO),
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ]
        try:
            completed = subprocess.run(
                command, check=True, capture_output=True, text=True, timeout=30
            )
        except subprocess.CalledProcessError as exc:
            pytest.fail(
                f"finding the main tree of {self.REPO} with git rev-parse failed "
                f"with code {exc.returncode}: {exc.stderr}; pass --boba-bin <dir>"
            )

        main = Path(completed.stdout.strip()).parent
        return main / "release" / version("boba-cli") / "bin"


@pytest.fixture(scope="module")
def release_bin(request: pytest.FixtureRequest) -> ReleaseBin:
    return ReleaseBin(request.config.getoption("boba_bin", default=None))


class TestBinary:
    HELP_CEILING_SEC: ClassVar[float] = 10.0
    SOURCE_SNIPPETS: ClassVar[tuple[bytes, ...]] = (
        b"async def _guard_context",
        b"def _recover_overflow",
        b"class TurnLoop",
    )

    @pytest.mark.binary
    def test_help_starts_fast_and_holds_no_source_text(
        self, release_bin: ReleaseBin
    ) -> None:
        path = release_bin.path(ReleaseBinary.BOBA)
        started = time.monotonic()
        completed = subprocess.run(
            [str(path), "--help"], check=True, capture_output=True, timeout=60
        )
        elapsed = time.monotonic() - started
        size = path.stat().st_size

        assert b"--settings" in completed.stdout
        print(f"\n./boba --help: {elapsed:.2f} s, size {size / 1_000_000:.1f} MB")
        assert elapsed < self.HELP_CEILING_SEC

        data = path.read_bytes()
        for snippet in self.SOURCE_SNIPPETS:
            assert snippet not in data

    @pytest.mark.binary
    def test_self_check_finds_the_package_data(self, release_bin: ReleaseBin) -> None:
        """Опись сборки внутри, и все её файлы доступны: шаблоны агента и схема
        истории Postgres."""
        path = release_bin.path(ReleaseBinary.BOBA)
        completed = subprocess.run(
            [str(path), "self-check"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

        assert completed.returncode == 0, completed.stderr[-2000:]
        assert "package data files present" in completed.stdout
        assert "boba.history.postgres" in completed.stdout
        assert "boba.agent" in completed.stdout

    @pytest.mark.binary
    async def test_postgres_history_session_resumes_through_the_binary(  # noqa: PLR0913 — фикстуры стенда
        self,
        model: FakeModel,
        registration: StandRegistration,
        stand: PostgresStand,
        pool: AsyncPostgresPool,
        release_bin: ReleaseBin,
        tmp_path: Path,
    ) -> None:
        """История в Postgres стенда так, как её отдаёт агенту чат: строка libpq
        без пароля, вход билетом Kerberos из окружения. Бинарник создаёт схему
        истории своими файлами, ведёт ход с инструментом, после закрытия
        сессии возобновляет её с той же лентой и ведёт второй ход; записи обоих
        вопросов — в Postgres."""
        path = release_bin.path(ReleaseBinary.BOBA)
        boba = AcpProcess(tmp_path, model, [str(path)], stand.environment())
        prompt = call({"name": "mcp__stand__add", "arguments": {"a": 2, "b": 3}})
        view = FeedView()
        client = AcpClient()
        conn = await boba.start(client, registration, stand.settings(boba, None))
        try:
            await handshake(conn)
            session = await conn.new_session(
                cwd=str(tmp_path), mcp_servers=[StandRegistration.STAND]
            )
            session_id = session.session_id
            _, first = await boba.turn(client, session_id, prompt)
            assert first.stop_reason == StopReason.END_TURN

            live = client.updates_of(session_id)
            await conn.close_session(session_id=session_id)
            await conn.resume_session(
                cwd=str(tmp_path),
                session_id=session_id,
                mcp_servers=[StandRegistration.STAND],
                replay_from=schema.ReplayFromStartVariant(),
            )
            replayed = client.updates_of(session_id)[len(live) :]
            assert view.render(replayed) == view.render(live)

            answer = [schema.TextContentBlock(text=ScenarioName.ANSWER.value)]
            _, second = await boba.turn(client, session_id, answer)
            assert second.stop_reason == StopReason.END_TURN

            await conn.close_session(session_id=session_id)
            human = await stand.human_prompts(stand.store(pool), session_id)
            assert human == [prompt[0].text, ScenarioName.ANSWER.value]
        finally:
            await conn.close()
            await boba.close_stdin()
            code = await boba.wait()
            await boba.kill()

        assert code == 0
        boba.assert_stdout_is_frames_only()

    @pytest.mark.binary
    async def test_acp_scenario_runs_through_the_binary(
        self,
        model: FakeModel,
        registration: StandRegistration,
        release_bin: ReleaseBin,
        tmp_path: Path,
    ) -> None:
        path = release_bin.path(ReleaseBinary.BOBA)
        boba = AcpProcess(tmp_path, model, [str(path)])
        client = AcpClient()
        conn = await boba.start(client, registration, None, "--record-requests")
        try:
            await handshake(conn)
            session = await conn.new_session(
                cwd=str(tmp_path), mcp_servers=[StandRegistration.STAND]
            )
            await Steps.tool_turn_streams_updates(boba, client, session.session_id)
            await Steps.permission_is_asked_and_remembered(
                boba, client, session.session_id
            )
        finally:
            await conn.close()
            await boba.close_stdin()
            code = await boba.wait()
            await boba.kill()

        assert code == 0
        boba.assert_stdout_is_frames_only()
        types = [r["type"] for r in boba.journal(session.session_id)]
        assert "api-request" in types
        assert "permission-rule" in types

    @pytest.mark.binary_onnx
    async def test_local_model_answers_inside_the_binary(
        self,
        raw_config: DictConfig,
        registration: StandRegistration,
        release_bin: ReleaseBin,
        tmp_path: Path,
    ) -> None:
        """Рантайм onnxruntime-genai внутри файла: ход на локальной модели стенда
        проходит, веса лежат снаружи и называются профилем."""
        model_dir = str(raw_config["agent_stand"]["model_dir"])
        fake = FakeModel()
        onnx = release_bin.path(ReleaseBinary.ONNX)
        boba = AcpProcess(tmp_path, fake, [str(onnx)])
        document = boba.settings_document()
        document["model"] = "local"
        document["models"] = {
            "local": {
                "provider": {"kind": "onnx", "modelDir": model_dir},
                "model": "qwen3",
                "sampling": {"max_tokens": 256},
                "contextWindow": 8000,
                "maxOutputTokens": 256,
            }
        }
        client = AcpClient()
        conn = await boba.start(
            client,
            registration,
            document,
            "--system-prompt",
            "Answer in one short sentence.",
        )
        try:
            await handshake(conn)
            session = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
            _, idle = await boba.turn(
                client,
                session.session_id,
                [schema.TextContentBlock(text="Say hello in one word.")],
            )
            assert idle.stop_reason == StopReason.END_TURN
        finally:
            await conn.close()
            await boba.close_stdin()
            code = await boba.wait()
            await boba.kill()

        assert code == 0
        assistants = [
            r for r in boba.journal(session.session_id) if r["type"] == "assistant"
        ]
        assert assistants
        assert any(r["message"]["usage"]["output_tokens"] > 0 for r in assistants)
