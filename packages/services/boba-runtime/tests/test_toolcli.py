"""CLI хоста: модуль по имени, injected из toml, --injected напрямую, отказы argv."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from boba.toolkit.entry import ToolMain


class TestToolCli:
    MODULE = "boba.stand.fake_toolmod"

    @staticmethod
    def run_cli(arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(  # noqa: S603
            [sys.executable, "-m", "boba.runtime.toolcli", *arguments],
            capture_output=True,
            env=dict(os.environ),
            check=False,
        )

    @staticmethod
    def write_toml(tmp_path: Path) -> Path:
        config = tmp_path / "config.toml"
        config.write_text('[tool.fake]\ntoken = "s3cret-token"\nlimit = 5\n')

        return config

    def test_injected_is_built_from_toml_sections(self, tmp_path: Path) -> None:
        config = self.write_toml(tmp_path)

        proc = self.run_cli(
            [
                self.MODULE,
                "fake_echo",
                "--text",
                "ping",
                "--repeat",
                "2",
                "--config",
                str(config),
            ]
        )

        assert proc.returncode == ToolMain.Exit.OK, proc.stderr
        assert b"ping ping|s3cret-token" in proc.stdout

    def test_injected_file_wins_over_toml(self, tmp_path: Path) -> None:
        config = self.write_toml(tmp_path)
        injected = tmp_path / "injected.json"
        injected.write_text('{"cfg": {"token": "from-file", "limit": 1}}')

        proc = self.run_cli(
            [
                self.MODULE,
                "fake_echo",
                "--text",
                "pong",
                "--repeat",
                "1",
                "--config",
                str(config),
                "--injected",
                str(injected),
                "--artifact",
            ]
        )

        assert proc.returncode == ToolMain.Exit.OK, proc.stderr
        assert b"from-file" in proc.stdout

    def test_missing_config_is_entry_error(self) -> None:
        proc = self.run_cli([self.MODULE, "fake_echo", "--text", "x"])

        assert proc.returncode == ToolMain.Exit.ENTRY_ERROR
        assert b"--config" in proc.stderr

    def test_unknown_module_is_entry_error(self, tmp_path: Path) -> None:
        config = self.write_toml(tmp_path)

        proc = self.run_cli(["boba.no_such", "x", "--config", str(config)])

        assert proc.returncode == ToolMain.Exit.ENTRY_ERROR
        assert b"not importable" in proc.stderr
