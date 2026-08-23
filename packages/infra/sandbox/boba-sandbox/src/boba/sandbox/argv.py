"""Pure builder: SandboxProfile -> argv запуска зиготы под bwrap."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TypeVar

from boba.sandbox.profile import BindSpec, SandboxMount, SandboxProfile
from boba.toolkit.binaries import SandboxBinary
from boba.workspace.launcher import FUSE_DEVICE

__all__ = ["build_zygote_argv"]


def build_zygote_argv(
    profile: SandboxProfile,
    command: list[str],
    *,
    env: Mapping[str, str],
    root: str,
) -> list[str]:
    """bwrap для зиготы внутри userns лаунчера образов.

    CAP_SYS_ADMIN — unshare и mount детей, CAP_SYS_RESOURCE — запрет
    вложенных userns записью max_user_namespaces=0, CAP_SETPCAP — сброс
    bounding set перед запуском тела. Свой userns не создаётся: цепочка
    лаунчера уже в нём, зато его капабилити наследуются. root — точка,
    куда цепочка смонтировала образ корня; сеть отбирает внешний bwrap.
    """
    bwrap_path = profile.host.binaries.resolve(SandboxBinary.BWRAP)

    argv = [
        bwrap_path,
        "--die-with-parent",
        "--as-pid-1",
        "--cap-add",
        "CAP_SYS_ADMIN",
        "--cap-add",
        "CAP_SYS_RESOURCE",
        "--cap-add",
        "CAP_SETPCAP",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup-try",
        # внутреннее устройство телу не видно
        "--hostname",
        "sandbox",
        "--new-session",
        "--ro-bind",
        root,
        "/",
    ]

    argv += ["--proc", SandboxMount.PROC.value]
    argv += ["--dev", SandboxMount.DEV.value]

    argv += ["--dev-bind", FUSE_DEVICE, FUSE_DEVICE]

    # tmpfs раньше биндов: на read-only корне bwrap не создаст точку
    # монтирования, а поверх tmpfs — создаст
    for spec in profile.tmpfs():
        argv += ["--size", str(spec.size_bytes), "--tmpfs", spec.path]

    setup = profile.setup_binds()
    symlinks: list[tuple[str, str]] = []

    ro_binds = (*profile.mounts.ro, *setup.ro)
    for spec in ro_binds:
        argv += _bind_args("--ro-bind-try", spec, symlinks)

    rw_binds = (*profile.mounts.rw, *setup.rw)
    for spec in _dedup_preserve_order(rw_binds):
        argv += _bind_args("--bind-try", spec, symlinks)

    for target, link_path in _dedup_preserve_order(symlinks):
        argv += ["--symlink", target, link_path]

    argv += ["--clearenv"]
    for name, value in env.items():
        argv += ["--setenv", name, value]

    # cwd профиля может быть точкой образа, которую смонтирует ребёнок
    argv += ["--chdir", "/"]
    argv += ["--", *command]
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
