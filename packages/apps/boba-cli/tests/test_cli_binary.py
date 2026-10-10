"""Бинарник `release/<версия>/bin/boba` (pytest -m integration): сценарий
этапа 6 через него, взаимное возобновление журналов командой и бинарником,
время старта и размер, отсутствие исходного текста внутри, локальная модель
onnx внутри файла. Без собранного бинарника тесты пропускаются:
`make -C build boba-bin`."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import AsyncIterator
from importlib.metadata import version
from pathlib import Path

import pytest
from omegaconf import DictConfig
from test_cli_process import BobaProcess, FakeModel, is_result, journal_lines, user
from test_cli_process import TestConversation as Steps

from boba.stand.ui.fake_llm import ScenarioName

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


@pytest.fixture
async def model() -> AsyncIterator[FakeModel]:
    fake = FakeModel()
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


class TestBinary:
    def test_help_starts_fast_and_holds_no_source_text(self) -> None:
        path = binary()
        started = time.monotonic()
        completed = subprocess.run(
            [str(path), "--help"], check=True, capture_output=True, timeout=60
        )
        elapsed = time.monotonic() - started
        size = path.stat().st_size

        assert b"--session" in completed.stdout
        print(f"\n./boba --help: {elapsed:.2f} s, size {size / 1_000_000:.1f} MB")
        assert elapsed < HELP_CEILING_SEC

        data = path.read_bytes()
        for snippet in SOURCE_SNIPPETS:
            assert snippet not in data

    async def test_stage_six_scenario_runs_through_the_binary(
        self, model: FakeModel, tmp_path: Path
    ) -> None:
        boba = BobaProcess(tmp_path, model, [str(binary())])
        try:
            await boba.start(
                "--session",
                "bin-1",
                "--mcp-config",
                boba.mcp_config(),
                "--record-requests",
            )
            init = await boba.read()
            assert init["subtype"] == "init"
            assert "add" in init["tools"]

            await Steps.tool_runs_without_a_question(boba)
            await Steps.permission_is_asked_and_granted(boba)
            await Steps.plan_mode_denies_the_tool(boba)
            await Steps.late_message_is_cancelled(boba)
            await Steps.interrupt_ends_the_turn(boba)

            await boba.close_stdin()
            assert await boba.wait() == 0
            assert boba.stray == []
        finally:
            await boba.kill()

        types = [r["type"] for r in journal_lines(tmp_path, "bin-1")]
        assert "api-request" in types
        assert "permission-rule" in types

    async def test_journals_resume_across_the_command_and_the_binary(
        self, model: FakeModel, tmp_path: Path
    ) -> None:
        command = BobaProcess(tmp_path, model)
        await command.start("--session", "bin-2")
        try:
            await command.read()
            await command.send(user(ScenarioName.ANSWER.value))
            await command.read_until(is_result)
            await command.close_stdin()
            assert await command.wait() == 0
        finally:
            await command.kill()

        compiled = BobaProcess(tmp_path, model, [str(binary())])
        await compiled.start("--session", "bin-2", "--resume")
        try:
            init = await compiled.read()
            assert init["resumed"] is True
            await compiled.send(user(ScenarioName.THINKING.value))
            result = await compiled.read_until(is_result)
            assert result["subtype"] == "completed"
            await compiled.close_stdin()
            assert await compiled.wait() == 0
        finally:
            await compiled.kill()

        again = BobaProcess(tmp_path, model)
        await again.start("--session", "bin-2", "--resume")
        try:
            init = await again.read()
            assert init["resumed"] is True
            await again.send(user(ScenarioName.ANSWER.value))
            result = await again.read_until(is_result)
            assert result["subtype"] == "completed"
            await again.close_stdin()
            assert await again.wait() == 0
        finally:
            await again.kill()

        requests = model.app.requests
        assert len(requests) == 3
        assert (
            requests[2]["messages"][: len(requests[1]["messages"])]
            == requests[1]["messages"]
        )

    async def test_local_model_answers_inside_the_binary(
        self, raw_config: DictConfig, tmp_path: Path
    ) -> None:
        """Рантайм onnxruntime-genai внутри файла: ход на локальной модели стенда
        проходит, веса лежат снаружи и называются профилем."""
        model_dir = str(raw_config["agent_stand"]["model_dir"])
        settings = {
            "model": "local",
            "models": {
                "local": {
                    "provider": {"kind": "onnx", "modelDir": model_dir},
                    "model": "qwen3",
                    "sampling": {"max_tokens": 256},
                    "contextWindow": 8000,
                    "maxOutputTokens": 256,
                }
            },
        }
        fake = FakeModel()
        boba = BobaProcess(tmp_path, fake, [str(binary())])
        try:
            await boba.start(
                "--session",
                "bin-onnx",
                "--settings",
                json.dumps(settings),
                "--system-prompt",
                "Answer in one short sentence.",
            )
            init = await boba.read()
            assert init["model"] == "qwen3"
            await boba.send(user("Say hello in one word."))
            result = await boba.read_until(is_result)
            assert result["subtype"] in ("completed", "api_error")
            await boba.close_stdin()
            assert await boba.wait() == 0
        finally:
            await boba.kill()

        assistants = [
            r for r in journal_lines(tmp_path, "bin-onnx") if r["type"] == "assistant"
        ]
        assert assistants
        assert any(r["message"]["usage"]["output_tokens"] > 0 for r in assistants)
