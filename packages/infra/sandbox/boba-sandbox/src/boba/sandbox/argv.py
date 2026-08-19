"""Pure builder: SandboxProfile + command -> argv для запуска bwrap."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TypeVar

from boba.sandbox.profile import BindSpec, SandboxProfile
from boba.toolkit.binaries import SandboxBinary

__all__ = ["WORKSPACE_MOUNT", "build_bwrap_argv", "build_zygote_argv"]


WORKSPACE_MOUNT = "/workspace"
"""Конвенция: target рабочей папки чата в rw_binds; сюда смотрят вложения."""


def build_bwrap_argv(
    profile: SandboxProfile,
    command: str,
    *,
    env: Mapping[str, str],
    nested: bool = False,
) -> list[str]:
    """nested — запуск внутри userns лаунчера образов: свой userns недоступен."""
    argv = _bwrap_preamble(profile, nested)

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
        argv += ["--size", str(spec.size_bytes), "--tmpfs", spec.path]

    argv += ["--clearenv"]
    for name, value in env.items():
        argv += ["--setenv", name, value]

    argv += ["--chdir", profile.cwd or "/"]

    guarded = f"ulimit -u {profile.max_processes} || exit 1; {command}"
    argv += ["--", "/bin/bash", "-c", guarded]
    return argv


def build_zygote_argv(
    profile: SandboxProfile,
    command: list[str],
    *,
    env: Mapping[str, str],
) -> list[str]:
    """bwrap для зиготы: capabilities в своём userns, команда без bash-guard.

    CAP_SYS_ADMIN — unshare и mount детей, CAP_SYS_RESOURCE — запрет
    вложенных userns записью max_user_namespaces=0. Guard `ulimit -u` не
    ставится: процессы вызова ограничивает pids.max его cgroup-leaf'а.
    """
    bwrap_path = profile.binaries.resolve(SandboxBinary.BWRAP)

    argv = [
        bwrap_path,
        "--die-with-parent",
        "--unshare-user",
        "--uid",
        "0",
        "--gid",
        "0",
        "--cap-add",
        "CAP_SYS_ADMIN",
        "--cap-add",
        "CAP_SYS_RESOURCE",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup-try",
        "--hostname",
        "zygote",
        "--new-session",
    ]

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
        argv += ["--size", str(spec.size_bytes), "--tmpfs", spec.path]

    argv += ["--clearenv"]
    for name, value in env.items():
        argv += ["--setenv", name, value]

    if not profile.network:
        argv.append("--unshare-net")

    argv += ["--chdir", profile.cwd or "/"]
    argv += ["--", *command]
    return argv


def _bwrap_preamble(profile: SandboxProfile, nested: bool) -> list[str]:
    bwrap_path = profile.binaries.resolve(SandboxBinary.BWRAP)

    argv = [
        bwrap_path,
        "--die-with-parent",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--hostname",
        "sandbox",
        "--unshare-cgroup-try",
        "--new-session",
    ]
    if nested:
        argv += ["--cap-drop", "ALL"]
        return argv
    argv.insert(2, "--unshare-user")
    argv.append("--disable-userns")
    if not profile.network:
        argv.append("--unshare-net")
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
    seen: set[_T] = set()
    out: list[_T] = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out
