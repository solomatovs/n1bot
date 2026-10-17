"""Собранный бинарник `release/<версия>/bin/boba`: сценарий ACP через него —
рукопожатие, сессия с регистрацией MCP-over-ACP, ход с инструментом,
разрешение; время старта и размер, отсутствие исходного текста внутри.
Локальную модель onnx проверяет `bin/boba-onnx` — сборка с рантаймом
onnxruntime-genai.

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
    StandRegistration,
    Steps,
    handshake,
)

from boba.transport.acp.protocol import StopReason

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


class ReleaseBinary(StrEnum):
    """Бинарники выпуска в каталоге bin и команды, которые их собирают."""

    BOBA = "boba"
    ONNX = "boba-onnx"

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
            [str(path), "acp", "--help"], check=True, capture_output=True, timeout=60
        )
        elapsed = time.monotonic() - started
        size = path.stat().st_size

        assert b"--settings" in completed.stdout
        print(f"\n./boba acp --help: {elapsed:.2f} s, size {size / 1_000_000:.1f} MB")
        assert elapsed < self.HELP_CEILING_SEC

        data = path.read_bytes()
        for snippet in self.SOURCE_SNIPPETS:
            assert snippet not in data

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
