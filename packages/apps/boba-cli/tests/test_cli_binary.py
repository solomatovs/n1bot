"""Бинарник `release/<версия>/bin/boba` (pytest -m integration): сценарий ACP
через него — рукопожатие, сессия с регистрацией MCP-over-ACP, ход с инструментом,
разрешение; время старта и размер, отсутствие исходного текста внутри,
локальная модель onnx внутри файла. Без собранного бинарника тесты
пропускаются: `make -C build boba-bin`."""

from __future__ import annotations

import subprocess
import time
from importlib.metadata import version
from pathlib import Path

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

REPO = Path(__file__).resolve().parents[4]
BINARY = REPO / "release" / version("boba-cli") / "bin" / "boba"
HELP_CEILING_SEC = 10.0
SOURCE_SNIPPETS = (
    b"async def _guard_context",
    b"def _recover_overflow",
    b"class TurnLoop",
)


def binary() -> Path:
    if not BINARY.is_file():
        pytest.skip(f"no binary at {BINARY}; run: make -C build boba-bin")

    return BINARY


class TestBinary:
    def test_help_starts_fast_and_holds_no_source_text(self) -> None:
        path = binary()
        started = time.monotonic()
        completed = subprocess.run(
            [str(path), "acp", "--help"], check=True, capture_output=True, timeout=60
        )
        elapsed = time.monotonic() - started
        size = path.stat().st_size

        assert b"--settings" in completed.stdout
        print(f"\n./boba acp --help: {elapsed:.2f} s, size {size / 1_000_000:.1f} MB")
        assert elapsed < HELP_CEILING_SEC

        data = path.read_bytes()
        for snippet in SOURCE_SNIPPETS:
            assert snippet not in data

    async def test_acp_scenario_runs_through_the_binary(
        self, model: FakeModel, registration: StandRegistration, tmp_path: Path
    ) -> None:
        boba = AcpProcess(tmp_path, model, [str(binary())])
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

    async def test_local_model_answers_inside_the_binary(
        self, raw_config: DictConfig, registration: StandRegistration, tmp_path: Path
    ) -> None:
        """Рантайм onnxruntime-genai внутри файла: ход на локальной модели стенда
        проходит, веса лежат снаружи и называются профилем."""
        model_dir = str(raw_config["agent_stand"]["model_dir"])
        fake = FakeModel()
        boba = AcpProcess(tmp_path, fake, [str(binary())])
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
