"""Программа запускается модулем и читает журнал: пустая проверка этапа 0."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


class TestPromptAssemblyCli:
    def test_help_lists_commands(self) -> None:
        done = subprocess.run(
            [sys.executable, "-m", "boba.prompt_assembly", "--help"],
            capture_output=True,
            text=True,
            check=False,
        )

        assert done.returncode == 0, done.stderr
        for command in ("history", "build", "compaction", "verify"):
            assert command in done.stdout

    def test_history_reads_a_minimal_journal(self, tmp_path: Path) -> None:
        journal = tmp_path / "s.jsonl"
        user = {
            "type": "user",
            "uuid": "u1",
            "parentUuid": None,
            "sessionId": "s",
            "timestamp": "2026-10-10T00:00:00.000Z",
            "isSidechain": False,
            "userType": "external",
            "cwd": str(tmp_path),
            "version": "2.1.289",
            "gitBranch": "",
            "message": {"role": "user", "content": "hello"},
        }
        assistant = {
            "type": "assistant",
            "uuid": "a1",
            "parentUuid": "u1",
            "sessionId": "s",
            "timestamp": "2026-10-10T00:00:01.000Z",
            "isSidechain": False,
            "userType": "external",
            "cwd": str(tmp_path),
            "version": "2.1.289",
            "gitBranch": "",
            "requestId": "req_1",
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "m",
                "content": [{"type": "text", "text": "hi"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        }
        lines = [json.dumps(user), json.dumps(assistant)]
        journal.write_text("\n".join(lines) + "\n", encoding="utf-8")

        done = subprocess.run(
            [sys.executable, "-m", "boba.prompt_assembly", "history", str(journal)],
            capture_output=True,
            text=True,
            check=False,
        )

        assert done.returncode == 0, done.stderr
        assert "hello" in done.stdout or "u1" in done.stdout
