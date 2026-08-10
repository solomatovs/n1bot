"""Pure builder: SandboxProfile + command -> argv для запуска bwrap.

Старый путь (`build_bwrap_argv`) кладёт профиль в argv целиком; канальный
(`ChannelArgv.build`) уводит профиль nul-separated в канал wrap_args
(`--args FD`), в argv остаётся только команда.

Ошибки: LauncherError — bwrap отсутствует в PATH или нарушен контракт сборки.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import ClassVar, TypeVar

from boba.sandbox.profile import BindSpec, SandboxProfile
from boba.toolkit.launcher import LauncherError

__all__ = [
    "WORKSPACE_MOUNT",
    "ChannelArgv",
    "WrapArgsCodec",
    "build_bwrap_argv",
]


WORKSPACE_MOUNT = "/workspace"
"""Конвенция: target рабочей папки чата в rw_binds; сюда смотрят вложения."""


class WrapArgsCodec:
    """Кодирование опций bwrap для каналов wrap_args/wrap_args_inner.

    Формат задаёт `bwrap --args FD`: каждая опция завершается nul-байтом.
    """

    SEPARATOR: ClassVar[bytes] = b"\0"
    ENCODING: ClassVar[str] = "utf-8"

    @classmethod
    def encode(cls, options: Sequence[str]) -> bytes:
        if not options:
            return b""

        chunks: list[bytes] = []
        for option in options:
            chunks.append(option.encode(cls.ENCODING))

        return cls.SEPARATOR.join(chunks) + cls.SEPARATOR


@dataclass(frozen=True)
class ChannelArgv:
    """Канальная сборка запуска: короткий argv и профиль для wrap_args."""

    argv: tuple[str, ...]
    wrap_args: bytes

    @classmethod
    def build(  # noqa: PLR0913
        cls,
        profile: SandboxProfile,
        command: str,
        *,
        env: Mapping[str, str],
        wrap_args_fd: int,
        redirect_prefix: str,
        nested: bool = False,
    ) -> ChannelArgv:
        """Профиль уезжает в wrap_args; argv не растёт с профилем и не виден в ps.

        env обязан уже содержать пары `Channel.env_name` нужных узлу каналов —
        они едут `--setenv`-опциями внутри wrap_args. redirect_prefix
        (`exec >&N 2>&M`) стоит после ulimit: отказ ulimit — сообщение
        обвязки, оно уходит в wrap_stderr до перенаправления.
        """
        if not redirect_prefix:
            msg = "redirect prefix is required for the channel path"
            raise LauncherError(msg)

        options = _profile_options(profile, env=env, nested=nested)

        guarded = (
            f"ulimit -u {profile.max_processes} || exit 1; "
            f"{redirect_prefix}; {command}"
        )

        argv = (
            _bwrap_path(),
            "--args",
            str(wrap_args_fd),
            "--",
            "/bin/bash",
            "-c",
            guarded,
        )

        return cls(argv=argv, wrap_args=WrapArgsCodec.encode(options))


def build_bwrap_argv(
    profile: SandboxProfile,
    command: str,
    *,
    env: Mapping[str, str],
    nested: bool = False,
) -> list[str]:
    """nested — запуск внутри userns лаунчера образов: свой userns недоступен."""
    argv = [_bwrap_path()]
    argv += _profile_options(profile, env=env, nested=nested)

    guarded = f"ulimit -u {profile.max_processes} || exit 1; {command}"
    argv += ["--", "/bin/bash", "-c", guarded]

    return argv


def _bwrap_path() -> str:
    path = shutil.which("bwrap")

    if path is None:
        raise LauncherError("bwrap not found in PATH")

    return path


def _profile_options(
    profile: SandboxProfile,
    *,
    env: Mapping[str, str],
    nested: bool,
) -> list[str]:
    """Все опции профиля в порядке старой сборки: изоляция -> mounts -> env -> cwd."""
    options = _isolation_options(profile, nested)

    if profile.rootfs:
        options += ["--ro-bind", profile.rootfs, "/"]

    options += ["--proc", "/proc", "--dev", "/dev"]

    symlinks: list[tuple[str, str]] = []

    for spec in profile.ro_binds:
        options += _bind_args("--ro-bind-try", spec, symlinks)

    for spec in _dedup_preserve_order(profile.rw_binds):
        options += _bind_args("--bind-try", spec, symlinks)

    for target, link_path in _dedup_preserve_order(symlinks):
        options += ["--symlink", target, link_path]

    for spec in profile.tmpfs:
        options += ["--size", str(spec.size_bytes), "--tmpfs", spec.path]

    options += ["--clearenv"]
    for name, value in env.items():
        options += ["--setenv", name, value]

    cwd = profile.cwd
    if not cwd:
        cwd = "/"

    options += ["--chdir", cwd]

    return options


def _isolation_options(profile: SandboxProfile, nested: bool) -> list[str]:
    options = [
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
        options += ["--cap-drop", "ALL"]

        # сеть внешней ступени mount-группы — OR стадий; стадия без сети
        # изолируется здесь, вложенным unshare
        if not profile.network:
            options.append("--unshare-net")

        return options

    options.insert(1, "--unshare-user")
    options.append("--disable-userns")

    if not profile.network:
        options.append("--unshare-net")

    return options


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
