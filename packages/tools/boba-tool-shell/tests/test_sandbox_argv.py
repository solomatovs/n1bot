"""Unit-тесты pure-builder'а argv для bwrap.

Не требуют установленного bwrap — проверяют только структуру argv.
"""

from __future__ import annotations

import pytest

from boba.tool.shell._profile import SandboxProfile
from boba.tool.shell._sandbox import build_bwrap_argv

_WS = "/srv/workspace"


def _split_after(argv: list[str], marker: str) -> list[str]:
    return argv[argv.index(marker):]


def test_starts_with_bwrap_and_unshare_flags():
    argv = build_bwrap_argv(
        SandboxProfile(),
        "echo hi",
        workspace_root=_WS,
        env={},
    )
    assert argv[0] == "bwrap"
    assert "--die-with-parent" in argv
    assert "--unshare-user" in argv
    assert "--unshare-pid" in argv
    assert "--new-session" in argv


def test_network_disabled_adds_unshare_net():
    argv = build_bwrap_argv(
        SandboxProfile(network=False),
        "true",
        workspace_root=_WS,
        env={},
    )
    assert "--unshare-net" in argv


def test_network_enabled_omits_unshare_net():
    argv = build_bwrap_argv(
        SandboxProfile(network=True),
        "true",
        workspace_root=_WS,
        env={},
    )
    assert "--unshare-net" not in argv


def test_ro_binds_emitted_with_ro_bind_try():
    # /usr и /etc — стабильные директории без symlink-pivot.
    profile = SandboxProfile(ro_binds=("/usr", "/etc"))
    argv = build_bwrap_argv(profile, "true", workspace_root=_WS, env={})
    for path in ("/usr", "/etc"):
        idx = argv.index(path)
        assert argv[idx - 1] == "--ro-bind-try"
        assert argv[idx + 1] == path


def test_ro_binds_passed_through_unchanged():
    # build_bwrap_argv не делает path-canonicalize; это работа
    # SandboxProfile._canonicalize_paths (field_validator). Поэтому
    # путь в argv — это уже резолвенный из профиля, без модификации
    # builder'ом.
    profile = SandboxProfile(ro_binds=("/usr",))
    argv = build_bwrap_argv(profile, "true", workspace_root=_WS, env={})
    expected = profile.ro_binds[0]
    idx = argv.index(expected)
    assert argv[idx - 1] == "--ro-bind-try"


def test_workspace_root_is_rw_bound_even_without_explicit_rw():
    profile = SandboxProfile(rw_binds=())
    argv = build_bwrap_argv(profile, "true", workspace_root=_WS, env={})
    idx = argv.index(_WS)
    assert argv[idx - 1] == "--bind-try"
    assert argv[idx + 1] == _WS


def test_workspace_root_not_duplicated_when_in_rw_binds():
    profile = SandboxProfile(rw_binds=(_WS,))
    argv = build_bwrap_argv(profile, "true", workspace_root=_WS, env={})
    assert argv.count("--bind-try") == sum(1 for x in argv if x == _WS) // 2


def test_tmpfs_mountpoints():
    profile = SandboxProfile(tmpfs=("/tmp", "/run"))  # noqa: S108 — пути внутри ns
    argv = build_bwrap_argv(profile, "true", workspace_root=_WS, env={})
    pairs = [
        (argv[i], argv[i + 1]) for i in range(len(argv) - 1)
        if argv[i] == "--tmpfs"
    ]
    assert ("--tmpfs", "/tmp") in pairs  # noqa: S108
    assert ("--tmpfs", "/run") in pairs


def test_env_clearenv_then_setenv():
    argv = build_bwrap_argv(
        SandboxProfile(),
        "true",
        workspace_root=_WS,
        env={"PATH": "/usr/bin", "FOO": "bar"},
    )
    assert "--clearenv" in argv
    clearenv_idx = argv.index("--clearenv")
    # все --setenv идут после --clearenv
    setenv_indices = [i for i, x in enumerate(argv) if x == "--setenv"]
    assert setenv_indices
    assert all(i > clearenv_idx for i in setenv_indices)
    # PATH=/usr/bin, FOO=bar
    pairs = [
        (argv[i + 1], argv[i + 2]) for i in setenv_indices
    ]
    assert ("PATH", "/usr/bin") in pairs
    assert ("FOO", "bar") in pairs


def test_chdir_defaults_to_workspace_root():
    argv = build_bwrap_argv(
        SandboxProfile(),
        "true",
        workspace_root=_WS,
        env={},
    )
    idx = argv.index("--chdir")
    assert argv[idx + 1] == _WS


def test_chdir_uses_profile_cwd_when_set():
    profile = SandboxProfile(cwd="/tmp/work")  # noqa: S108 — путь внутри ns
    argv = build_bwrap_argv(profile, "true", workspace_root=_WS, env={})
    idx = argv.index("--chdir")
    assert argv[idx + 1] == "/tmp/work"  # noqa: S108 — путь внутри ns


def test_command_passed_to_bash_c():
    argv = build_bwrap_argv(
        SandboxProfile(),
        "echo $((1+1))",
        workspace_root=_WS,
        env={},
    )
    tail = _split_after(argv, "--")
    assert tail == ["--", "/bin/bash", "-c", "echo $((1+1))"]


@pytest.mark.parametrize("dangerous", ["", "echo $(rm -rf /)", "\n; ls"])
def test_command_string_is_argv_safe(dangerous: str):
    # Команда едет как один argv-элемент, без shell-интерполяции в Python.
    argv = build_bwrap_argv(
        SandboxProfile(),
        dangerous,
        workspace_root=_WS,
        env={},
    )
    assert argv[-1] == dangerous
