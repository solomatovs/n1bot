"""Pure builder: SandboxProfile + command -> argv для запуска bwrap."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TypeVar

from boba.chainlit2.agent.tools.sandbox.profile import BindSpec, SandboxProfile

__all__ = ["WORKSPACE_MOUNT", "build_bwrap_argv"]

_BWRAP_BIN = "bwrap"
_BASH_BIN = "/bin/bash"

WORKSPACE_MOUNT = "/workspace"
"""Конвенция: target рабочей папки чата в rw_binds; сюда смотрят вложения."""


def build_bwrap_argv(
    profile: SandboxProfile,
    command: str,
    *,
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

    for spec in profile.ro_binds:
        argv += _bind_args("--ro-bind-try", spec, symlinks)

    for spec in _dedup_preserve_order(profile.rw_binds):
        argv += _bind_args("--bind-try", spec, symlinks)

    for target, link_path in _dedup_preserve_order(symlinks):
        argv += ["--symlink", target, link_path]

    for spec in profile.tmpfs:
        if spec.size_bytes:
            argv += ["--size", str(spec.size_bytes)]
        argv += ["--tmpfs", spec.path]

    argv += ["--clearenv"]
    for name, value in env.items():
        argv += ["--setenv", name, value]

    argv += ["--chdir", profile.cwd or "/"]

    argv += ["--", _BASH_BIN, "-c", command]
    return argv


def _bind_args(
    flag: str,
    spec: BindSpec,
    symlinks: list[tuple[str, str]],
) -> list[str]:
    """Явный target монтируется как есть; same-path bind чинит host-симлинки."""
    if spec.target != spec.host:
        return [flag, spec.host, spec.target]
    real, link = _resolve_bind(spec.host)
    if link is not None:
        symlinks.append(link)
    return [flag, real, real]


def _resolve_bind(path: str) -> tuple[str, tuple[str, str] | None]:
    if not os.path.islink(path):
        return path, None
    real = os.path.realpath(path)
    return real, (os.readlink(path), path)


_T = TypeVar("_T")


def _dedup_preserve_order(items: tuple[_T, ...] | list[_T]) -> list[_T]:
    seen: set[T] = set()
    out: list[T] = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out
