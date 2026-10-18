"""Pure builder: SandboxProfile -> argv запуска зиготы под bwrap."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence

from boba.sandbox.profile import BindSpec, SandboxMount, SandboxProfile
from boba.workspace.binaries import SandboxBinary
from boba.workspace.launcher import LayerPath, LayerSources

__all__ = ["ZygoteArgv"]


class ZygoteArgv:
    """argv вложенного bwrap зиготы по профилю песочницы и источники его биндов.

    Вложенный bwrap ищет каждый источник бинда в виде внешнего слоя цепочки
    лаунчера, поэтому внешний слой берёт их отсюда же, из объекта, который
    пишет argv: второго перечня источников нет. Создаёт его ZygoteSpawner и
    отдаёт источники построителю цепочки LauncherChain.
    """

    def __init__(self, profile: SandboxProfile) -> None:
        self._profile = profile

    def argv(
        self, command: Sequence[str], *, env: Mapping[str, str], root: str
    ) -> list[str]:
        """bwrap для зиготы внутри userns лаунчера образов.

        CAP_SYS_ADMIN — unshare и mount детей, CAP_SYS_RESOURCE — запрет
        вложенных userns записью max_user_namespaces=0, CAP_SETPCAP — сброс
        bounding set перед запуском тела. Свой userns не создаётся: цепочка
        лаунчера уже в нём, зато его капабилити наследуются. root — точка,
        куда цепочка смонтировала образ корня; сеть отбирает внешний bwrap.
        """
        bwrap_path = self._profile.host.binaries.resolve(SandboxBinary.BWRAP)

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

        argv += ["--dev-bind", LayerPath.FUSE.value, LayerPath.FUSE.value]

        # tmpfs раньше биндов: на read-only корне bwrap не создаст точку
        # монтирования, а поверх tmpfs — создаст
        for spec in self._profile.tmpfs():
            argv += ["--size", str(spec.size_bytes), "--tmpfs", spec.path]

        symlinks: list[tuple[str, str]] = []

        for spec in self._ro_binds():
            argv += ["--ro-bind-try", self._source(spec), self._target(spec)]
            symlinks.extend(self._link(spec))

        for spec in self._rw_binds():
            argv += ["--bind-try", self._source(spec), self._target(spec)]
            symlinks.extend(self._link(spec))

        for target, link_path in dict.fromkeys(symlinks):
            argv += ["--symlink", target, link_path]

        argv += ["--clearenv"]
        for name, value in env.items():
            argv += ["--setenv", name, value]

        # cwd профиля может быть точкой образа, которую смонтирует ребёнок
        argv += ["--chdir", "/"]
        argv += ["--", *command]
        return argv

    def sources(self) -> LayerSources:
        """Источники биндов ровно в том виде, в каком их пишет argv."""
        ro: list[str] = []
        for spec in self._ro_binds():
            ro.append(self._source(spec))

        rw: list[str] = []
        for spec in self._rw_binds():
            rw.append(self._source(spec))

        return LayerSources(ro=tuple(ro), rw=tuple(rw))

    def _ro_binds(self) -> tuple[BindSpec, ...]:
        setup = self._profile.setup_binds()
        return (*self._profile.mounts.ro, *setup.ro)

    def _rw_binds(self) -> tuple[BindSpec, ...]:
        setup = self._profile.setup_binds()
        return tuple(dict.fromkeys((*self._profile.mounts.rw, *setup.rw)))

    @staticmethod
    def _source(spec: BindSpec) -> str:
        """Явный target монтируется как есть; same-path bind чинит host-симлинки."""
        if spec.target != spec.host:
            return spec.host

        if not os.path.islink(spec.host):
            return spec.host

        return os.path.realpath(spec.host)

    def _target(self, spec: BindSpec) -> str:
        if spec.target != spec.host:
            return spec.target

        return self._source(spec)

    @staticmethod
    def _link(spec: BindSpec) -> tuple[tuple[str, str], ...]:
        if spec.target != spec.host:
            return ()

        if not os.path.islink(spec.host):
            return ()

        return ((os.readlink(spec.host), spec.host),)
