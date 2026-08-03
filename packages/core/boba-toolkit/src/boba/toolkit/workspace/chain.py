"""Pure builder: argv цепочки outer bwrap -> launcher для операций с образом."""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping, Sequence

from boba.toolkit.workspace.options import LauncherOptions, ResourceLimits

__all__ = [
    "FUSE_DEVICE",
    "LAUNCHER_ENV",
    "build_chain_argv",
    "render_image_path",
    "require_fuse",
]

_LAUNCHER_MODULE = "boba.toolkit.workspace.launcher"

FUSE_DEVICE = "/dev/fuse"

LAUNCHER_ENV = ("LD_LIBRARY_PATH", "PYTHONHOME", "PYTHONPATH")
"""Портативный python лаунчера без них не стартует: свои библиотеки и site."""


def require_fuse() -> None:
    """Проверяет предпосылки монтирования образов; падает громко и сразу."""
    if not os.path.exists(FUSE_DEVICE):
        msg = f"workspace: fuse is required, but {FUSE_DEVICE} is missing"
        raise RuntimeError(msg)
    if shutil.which("fuse2fs") is None:
        msg = "workspace: fuse2fs not found in PATH"
        raise RuntimeError(msg)
    if shutil.which("bwrap") is None:
        msg = "workspace: bwrap not found in PATH"
        raise RuntimeError(msg)


def build_chain_argv(  # noqa: PLR0913 — независимые параметры цепочки
    *,
    images: Sequence[tuple[str, str]],
    template: str,
    op: Sequence[str],
    python_bin: str,
    options: LauncherOptions,
    limits: ResourceLimits,
    rw_paths: Sequence[str] = (),
    network: bool = False,
    bwrap_bin: str = "bwrap",
) -> list[str]:
    """images — пары (образ, mountpoint); op — run/write/read/delete + аргумент.

    CAP_SYS_ADMIN действует только внутри создаваемого userns — на хосте
    процесс остаётся непривилегированным (это не docker --cap-add). Без него
    ядро не разрешит mount fuse, более узкой capability для mount(2) нет.
    После монтирования лаунчер сбрасывает caps; run идёт во вложенном bwrap
    с --cap-drop ALL, который свой userns/сеть создать уже не может — поэтому
    --unshare-net здесь. Хост отдаётся read-only (на запись — каталоги образов
    и rw_paths), из устройств виден только /dev/fuse, env чистится до того,
    что нужно самому лаунчеру (LAUNCHER_ENV).
    Создание новых userns блокирует сам лаунчер после mount (Launcher.USERNS_SYSCTL):
    bwrap --disable-userns несовместим с mount fuse изнутри песочницы.
    """
    bwrap_path = shutil.which(bwrap_bin)
    if not bwrap_path:
        msg = f"workspace: {bwrap_bin!r} not found in PATH"
        raise RuntimeError(msg)
    # относительные пути bwrap разрешает от корня песочницы, а он read-only:
    # точку монтирования создать не выйдет, поэтому всё приводим к абсолютным
    absolute: list[tuple[str, str]] = []
    for image, mnt in images:
        absolute.append((os.path.abspath(image), os.path.abspath(mnt)))
    images = absolute
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
        # CAP_SYS_RESOURCE (в userns) — право обнулить max_user_namespaces
        "--cap-add",
        "CAP_SYS_RESOURCE",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup-try",
        "--hostname",
        "sandbox",
        "--new-session",
        "--ro-bind",
        "/",
        "/",
    ]
    writable: list[str] = []
    for image, mnt in images:
        writable.append(os.path.dirname(image))
        writable.append(os.path.dirname(mnt))
    for path in rw_paths:
        writable.append(os.path.abspath(path))
    for path in dict.fromkeys(writable):
        argv += ["--bind", path, path]
    argv += [
        "--dev",
        "/dev",
        "--dev-bind",
        FUSE_DEVICE,
        FUSE_DEVICE,
        "--proc",
        "/proc",
        "--clearenv",
        "--setenv",
        "PATH",
        os.environ.get("PATH", "/usr/bin:/bin"),
    ]
    for name in LAUNCHER_ENV:
        value = os.environ.get(name)
        if not value:
            continue
        argv += ["--setenv", name, value]
    if not network:
        argv.append("--unshare-net")
    argv += [
        "--",
        python_bin,
        "-m",
        _LAUNCHER_MODULE,
        "--template",
        template,
        "--mount-wait-sec",
        str(options.mount_wait_sec),
        "--mount-poll-sec",
        str(options.mount_poll_sec),
        "--shutdown-wait-sec",
        str(options.shutdown_wait_sec),
        "--copy-chunk-bytes",
        str(options.copy_chunk_bytes),
        "--max-memory-bytes",
        str(limits.max_memory_bytes),
        "--max-cpu-sec",
        str(limits.max_cpu_sec),
        "--max-file-size-bytes",
        str(limits.max_file_size_bytes),
        "--max-open-files",
        str(limits.max_open_files),
        "--oom-score-adj",
        str(limits.oom_score_adj),
    ]
    for image, mnt in images:
        argv += ["--image", image, mnt]
    argv += list(op)
    return argv


def render_image_path(template: str, variables: Mapping[str, str]) -> str:
    try:
        return template.format_map(dict(variables))
    except KeyError as e:
        msg = f"workspace: variable {{{e.args[0]}}} in path {template!r} is not defined"
        raise RuntimeError(msg) from e
