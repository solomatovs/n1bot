"""CLI хоста: плагин по id, секция из toml на месте конфига, файл --args,
отказы командной строки."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from boba.toolkit.entry import ToolMain


class TestToolCli:
    PLUGIN = "bash"

    @staticmethod
    def run_cli(arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [sys.executable, "-m", "boba.runtime.toolcli", *arguments],
            capture_output=True,
            env=dict(os.environ),
            check=False,
        )

    @staticmethod
    def write_toml(tmp_path: Path, max_output_bytes: int = 65536) -> list[str]:
        """Аргументы файлов конфигурации: секция [tool.bash] и пустой site."""
        config = tmp_path / "config.toml"
        config.write_text(
            f"[tool.bash]\nmax_output_bytes = {max_output_bytes}\ntimeout_sec = 30\n"
        )
        site = tmp_path / "site.toml"
        site.write_text("")

        return ["--config", str(config), "--site", str(site)]

    @staticmethod
    def write_args(tmp_path: Path, args: dict[str, object]) -> list[str]:
        path = tmp_path / "args.json"
        path.write_text(json.dumps(args))

        return ["--args", str(path)]

    def test_section_is_served_from_toml(self, tmp_path: Path) -> None:
        config = self.write_toml(tmp_path)
        args = self.write_args(tmp_path, {"command": "echo toolcli-ping"})

        proc = self.run_cli([self.PLUGIN, "bash", *config, *args])

        assert proc.returncode == ToolMain.Exit.OK, proc.stderr
        assert b"toolcli-ping" in proc.stdout

    def test_args_file_wins_over_toml(self, tmp_path: Path) -> None:
        """Значение роли config в файле --args не заменяется секцией toml."""
        config = self.write_toml(tmp_path)
        cfg = {"max_output_bytes": 0, "timeout_sec": 30}
        args = self.write_args(tmp_path, {"command": "echo x", "cfg": cfg})

        proc = self.run_cli([self.PLUGIN, "bash", *config, *args])

        assert proc.returncode == ToolMain.Exit.ENTRY_ERROR
        assert b"max_output_bytes" in proc.stderr

    def test_missing_config_is_entry_error(self) -> None:
        proc = self.run_cli([self.PLUGIN, "bash"])

        assert proc.returncode == ToolMain.Exit.ENTRY_ERROR
        assert b"--config" in proc.stderr

    def test_unknown_plugin_is_entry_error(self, tmp_path: Path) -> None:
        config = self.write_toml(tmp_path)

        proc = self.run_cli(["no_such_plugin", "x", *config])

        assert proc.returncode == ToolMain.Exit.ENTRY_ERROR
        assert b"'no_such_plugin' is not installed" in proc.stderr

    def test_unknown_tool_is_entry_error(self, tmp_path: Path) -> None:
        config = self.write_toml(tmp_path)

        proc = self.run_cli([self.PLUGIN, "no_such_tool", *config])

        assert proc.returncode == ToolMain.Exit.ENTRY_ERROR
        assert b"unknown tool 'no_such_tool'" in proc.stderr
