"""Аргументы процесса: подкоманда, пути конфигов и абсолютные пути экземпляра."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

from boba.config.arguments import CommandSpec, ConfigArguments


def test_repeated_config_keeps_the_order_of_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    argv = [
        "prog",
        "serve",
        "--config",
        "/etc/boba/config.toml",
        "--config",
        "/srv/stand.toml",
    ]
    monkeypatch.setattr(sys, "argv", argv)

    call = ConfigArguments(
        argparse.ArgumentParser(prog="prog"),
        (CommandSpec("serve", "run service"),),
    ).call()

    assert call.command == "serve"
    assert call.paths == (Path("/etc/boba/config.toml"), Path("/srv/stand.toml"))


def test_health_takes_the_same_config(monkeypatch: pytest.MonkeyPatch) -> None:
    argv = ["prog", "health", "--config", "/etc/boba/config.toml"]
    monkeypatch.setattr(sys, "argv", argv)

    call = ConfigArguments(
        argparse.ArgumentParser(prog="prog"),
        (CommandSpec("health", "check service"),),
    ).call()

    assert call.command == "health"
    assert call.paths == (Path("/etc/boba/config.toml"),)


def test_missing_command_stops_the_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["prog", "--config", "/etc/boba/config.toml"])

    with pytest.raises(SystemExit):
        ConfigArguments(argparse.ArgumentParser(prog="prog"), ()).call()


def test_missing_config_stops_the_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["prog", "serve"])

    with pytest.raises(SystemExit):
        ConfigArguments(
            argparse.ArgumentParser(prog="prog"),
            (CommandSpec("serve", "run service"),),
        ).call()
