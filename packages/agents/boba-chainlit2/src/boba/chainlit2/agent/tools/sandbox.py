"""Pure builder: SandboxProfile + command -> argv для запуска bwrap."""

from __future__ import annotations

import os
from collections.abc import Mapping

from boba.chainlit2.agent.tools.sandbox_profile import SandboxProfile

__all__ = ["build_bwrap_argv"]

_BWRAP_BIN = "bwrap"
_BASH_BIN = "/bin/bash"

_WORKSPACE_MOUNT = "/workspace"


def build_bwrap_argv(
    profile: SandboxProfile,
    command: str,
    *,
    workspace_root: str,
    env: Mapping[str, str],
) -> list[str]:
    argv: list[str] = [
        _BWRAP_BIN,
        "--die-with-parent",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup-try",
        "--new-session",
    ]
    if not profile.network:
        argv.append("--unshare-net")

    if profile.rootfs:
        argv += ["--ro-bind", profile.rootfs, "/"]

    argv += ["--proc", "/proc", "--dev", "/dev"]

    symlinks: list[tuple[str, str]] = []

    for path in profile.ro_binds:
        real, link = _resolve_bind(path)
        argv += ["--ro-bind-try", real, real]
        if link is not None:
            symlinks.append(link)

    workspace_mount = _WORKSPACE_MOUNT if profile.rootfs else workspace_root
    argv += ["--bind-try", workspace_root, workspace_mount]

    for path in _dedup_preserve_order(profile.rw_binds):
        real, link = _resolve_bind(path)
        argv += ["--bind-try", real, real]
        if link is not None:
            symlinks.append(link)

    for target, link_path in _dedup_pairs(symlinks):
        argv += ["--symlink", target, link_path]

    for path in profile.tmpfs:
        argv += ["--tmpfs", path]

    argv += ["--clearenv"]
    for name, value in env.items():
        argv += ["--setenv", name, value]

    argv += ["--chdir", profile.cwd or workspace_mount]

    argv += ["--", _BASH_BIN, "-c", command]
    return argv


def _resolve_bind(path: str) -> tuple[str, tuple[str, str] | None]:
    if not os.path.islink(path):
        return path, None
    real = os.path.realpath(path)
    return real, (os.readlink(path), path)


def _dedup_preserve_order(items: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return tuple(out)


def _dedup_pairs(items: list[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out
