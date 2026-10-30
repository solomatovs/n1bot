"""Входы процесса: пути --config (конфиг экземпляра и наложения) и снимок
интерпретатора для цепочки лаунчера."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pytest

from boba.runtime.config import ConfigArguments, ProcessInterpreter


def test_repeated_config_keeps_the_order_of_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    argv = ["prog", "--config", "/etc/boba/config.toml", "--config", "/tmp/stand.toml"]
    monkeypatch.setattr(sys, "argv", argv)

    paths = ConfigArguments(argparse.ArgumentParser(prog="prog")).paths()

    assert paths == (Path("/etc/boba/config.toml"), Path("/tmp/stand.toml"))


def test_missing_config_stops_the_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["prog"])

    with pytest.raises(SystemExit):
        ConfigArguments(argparse.ArgumentParser(prog="prog")).paths()


def test_interpreter_snapshot_leaves_the_working_directory_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    interpreter = ProcessInterpreter().snapshot()

    assert os.path.isabs(interpreter.executable)
    for root in interpreter.roots:
        assert not str(tmp_path).startswith(root.rstrip("/") + "/")
        assert root != str(tmp_path)
