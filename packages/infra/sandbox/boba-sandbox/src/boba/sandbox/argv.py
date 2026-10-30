"""Pure builder: SandboxProfile -> argv запуска зиготы под bwrap."""

from __future__ import annotations

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

        # источник-ссылку bwrap разыменует сам, а точка в образе — та, что в
        # профиле: под systemd-resolved /etc/resolv.conf ведёт в /run, которого
        # в read-only корне образа не создать
        for spec in self._ro_binds():
            argv += ["--ro-bind-try", spec.host, spec.target]

        for spec in self._rw_binds():
            argv += ["--bind-try", spec.host, spec.target]

        # каталоги программы и данных: бинд без -try, нет источника или точки
        # в корне — зигота не стартует, и отказ называет путь
        for spec in self._profile.mounts.images:
            argv += ["--ro-bind", spec.host, spec.target]

        argv += ["--clearenv"]
        for name, value in env.items():
            argv += ["--setenv", name, value]

        # cwd профиля может быть точкой образа, которую смонтирует ребёнок
        argv += ["--chdir", "/"]
        argv += ["--", *command]
        return argv

    def sources(self) -> LayerSources:
        """Источники биндов ровно в том виде, в каком их пишет argv: каталоги
        программы и данных и ro-бинды — только на чтение."""
        ro: list[str] = []
        for spec in self._profile.mounts.images:
            ro.append(spec.host)

        for spec in self._ro_binds():
            ro.append(spec.host)

        rw: list[str] = []
        for spec in self._rw_binds():
            rw.append(spec.host)

        return LayerSources(ro=tuple(ro), rw=tuple(rw))

    def _ro_binds(self) -> tuple[BindSpec, ...]:
        setup = self._profile.setup_binds()
        return (*self._profile.mounts.ro, *setup.ro)

    def _rw_binds(self) -> tuple[BindSpec, ...]:
        setup = self._profile.setup_binds()
        return tuple(dict.fromkeys((*self._profile.mounts.rw, *setup.rw)))
