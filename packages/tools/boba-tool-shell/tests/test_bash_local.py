"""Тесты BashLocalTool (без sandbox-изоляции).

В отличие от sandbox-варианта, эти тесты не требуют bwrap — нужен
только `/bin/bash` на хосте.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from pathlib import Path

import pytest

from boba.tool.shell._profile_local import DEFAULT_PASSTHROUGH, resolve_local_env
from boba.tool.shell.bash_local import BashLocalTool
from boba.tool.shell.config import BashLocalConfig

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="bash отсутствует на хосте",
)


def _make_cfg(
    workspace_root: Path,
    *,
    cwd: str = "",
    env_passthrough: tuple[str, ...] = DEFAULT_PASSTHROUGH,
    env_set: Mapping[str, str] | None = None,
    timeout_sec: int = 30,
    max_output_bytes: int = 256 * 1024,
) -> BashLocalConfig:
    return BashLocalConfig(
        enable=True,
        workspace_root=workspace_root,
        cwd=cwd,
        env_passthrough=env_passthrough,
        env_set=dict(env_set or {}),
        timeout_sec=timeout_sec,
        max_output_bytes=max_output_bytes,
    )


def _exec(cfg: BashLocalConfig, **kwargs) -> dict:
    """Вызов tool как обычного callable. Возвращает payload (dict)."""
    return BashLocalTool()(cfg=cfg, **kwargs)


def test_echo_runs(tmp_path: Path):
    payload = _exec(_make_cfg(tmp_path), command="echo hello")
    assert payload["exit_code"] == 0
    assert payload["stdout"].rstrip() == "hello"
    assert not payload["timed_out"]


def test_cwd_defaults_to_workspace_root(tmp_path: Path):
    payload = _exec(_make_cfg(tmp_path), command="pwd")
    assert payload["stdout"].rstrip() == str(tmp_path.resolve())


def test_cwd_override(tmp_path: Path):
    sub = tmp_path / "sub"
    sub.mkdir()
    payload = _exec(_make_cfg(tmp_path, cwd=str(sub)), command="pwd")
    assert payload["stdout"].rstrip() == str(sub)


def test_workspace_writes_persist(tmp_path: Path):
    payload = _exec(_make_cfg(tmp_path), command="echo content > out.txt")
    assert payload["exit_code"] == 0
    assert (tmp_path / "out.txt").read_text() == "content\n"


def test_timeout_marks_timed_out(tmp_path: Path):
    payload = _exec(_make_cfg(tmp_path, timeout_sec=1), command="sleep 10")
    assert payload["timed_out"]
    assert payload["duration_ms"] < 5000


def test_output_truncation(tmp_path: Path):
    payload = _exec(
        _make_cfg(tmp_path, max_output_bytes=1024),
        command="yes x | head -c 10240",
    )
    assert payload["truncated_stdout"]
    assert len(payload["stdout"].encode("utf-8")) == 1024


def test_env_set_visible_in_command(tmp_path: Path):
    payload = _exec(
        _make_cfg(tmp_path, env_set={"FOO": "bar"}),
        command='echo "$FOO"',
    )
    assert payload["stdout"].rstrip() == "bar"


def test_default_passthrough_blocks_unknown_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("LEAK_ME", "secret")
    payload = _exec(_make_cfg(tmp_path), command='echo "${LEAK_ME:-empty}"')
    assert payload["stdout"].rstrip() == "empty"


def test_default_passthrough_lets_path_through(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    payload = _exec(_make_cfg(tmp_path), command='echo "$PATH"')
    assert payload["stdout"].rstrip() == "/usr/bin:/bin"


def test_explicit_passthrough_overrides_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("ALLOW_ME", "yes")
    payload = _exec(
        _make_cfg(
            tmp_path,
            env_passthrough=("ALLOW_ME",),
            env_set={"PATH": "/usr/bin:/bin"},
        ),
        command='echo "$ALLOW_ME"',
    )
    assert payload["stdout"].rstrip() == "yes"


def test_empty_passthrough_drops_host_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("HOME", "/home/leaked")
    payload = _exec(
        _make_cfg(
            tmp_path,
            env_passthrough=(),
            env_set={"PATH": "/usr/bin:/bin"},
        ),
        command='echo "${HOME:-empty}"',
    )
    assert payload["stdout"].rstrip() == "empty"


# --- pure unit tests of resolve_local_env (unchanged from v1) -------------


def test_resolve_local_env_default_uses_safe_allowlist():
    host = {"PATH": "/usr/bin", "HOME": "/h", "LEAK": "secret"}
    env = resolve_local_env(DEFAULT_PASSTHROUGH, {}, host)
    assert env == {"PATH": "/usr/bin", "HOME": "/h"}
    assert "LEAK" not in env


def test_resolve_local_env_passthrough_subset():
    env = resolve_local_env(("A",), {}, {"A": "1", "B": "2"})
    assert env == {"A": "1"}


def test_resolve_local_env_set_overrides_passthrough():
    env = resolve_local_env(
        ("A",),
        {"A": "overridden", "C": "new"},
        {"A": "1", "B": "2"},
    )
    assert env == {"A": "overridden", "C": "new"}


def test_resolve_local_env_empty_passthrough_drops_everything():
    assert resolve_local_env((), {}, {"A": "1"}) == {}


def test_default_passthrough_contains_expected_keys():
    # Защита от случайного broadening default-набора при рефакторе.
    assert set(DEFAULT_PASSTHROUGH) == {
        "PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM",
    }
