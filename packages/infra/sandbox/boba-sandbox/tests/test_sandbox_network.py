"""Сеть песочницы: профиль с network=true обязан резолвить имена.

Профили берутся из боевого конфига приложения тем же вызовом, что и загрузчик
плагинов, — проверяется ровно то окружение, в котором инструмент ходит в сеть.

Ошибка, ради которой написан тест: rootfs несёт собственный пустой
/etc/resolv.conf, и если host-файл не примонтирован поверх, getaddrinfo внутри
отвечает EAI_AGAIN («Temporary failure in name resolution») при живой сети —
именно так падал confluence_search, пока kb_fts_search ходил в базу по /etc/hosts.
"""

from __future__ import annotations

import os
import socket
from enum import StrEnum
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlsplit

import pytest
from omegaconf import DictConfig, OmegaConf

from boba.sandbox.profile import SandboxProfile, SandboxToolConfig
from boba.sandbox.runner import SandboxRunner, has_bwrap
from boba.settings import bind, build_app_config


class ResolverFile(StrEnum):
    """Файлы резолвера glibc: без них имя внутри песочницы не превратится в адрес."""

    RESOLV = "/etc/resolv.conf"
    HOSTS = "/etc/hosts"


class ProbeCommand(StrEnum):
    """Команды-пробы: и та и другая доступны в rootfs без bind'ов python."""

    RESOLVER = "cat /etc/resolv.conf"
    LOOKUP = "getent hosts {host}"

    def render(self, host: str) -> str:
        return self.value.format(host=host)


class SandboxToolProfiles:
    """Профили инструментов из боевого конфига: имя инструмента -> профиль."""

    CONFIG_ENV: ClassVar[str] = "BOBA_CONFIG_PATH"
    BASE_ENV: ClassVar[str] = "BOBA_BASE"
    CONFIG_IN_BASE: ClassVar[str] = "conf/config.toml"

    def __init__(self, raw: DictConfig) -> None:
        self._raw = raw

    @classmethod
    def config_path(cls) -> Path | None:
        """Тот же путь, что берёт приложение; None — конфига в среде нет."""
        if config_path := os.environ.get(cls.CONFIG_ENV):
            return Path(config_path)

        base = os.environ.get(cls.BASE_ENV)
        if not base:
            return None

        return Path(base) / cls.CONFIG_IN_BASE

    @classmethod
    def load(cls) -> SandboxToolProfiles | None:
        path = cls.config_path()
        if path is None:
            return None

        if not path.is_file():
            return None

        return cls(build_app_config(config_path=path))

    def networked(self) -> dict[str, SandboxProfile]:
        """Инструменты, которым конфиг разрешил сеть."""
        profiles: dict[str, SandboxProfile] = {}
        for name in self._tool_names():
            section = OmegaConf.select(self._raw, f"tool.{name}.sandbox")
            if section is None:
                continue

            profile = bind(self._raw, f"tool.{name}.sandbox", SandboxToolConfig)
            effective = profile.effective()
            if not effective.network:
                continue

            profiles[name] = effective

        return profiles

    def http_hosts(self) -> list[str]:
        """Хосты сервисов из секции [web.*]: по ним инструменты и ходят."""
        hosts: list[str] = []
        section = OmegaConf.select(self._raw, "web")
        if section is None:
            return hosts

        for profile in section.values():
            base_url = profile.get("base_url")
            if not base_url:
                continue

            host = urlsplit(str(base_url)).hostname
            if not host:
                continue

            hosts.append(host)

        return hosts

    def _tool_names(self) -> list[str]:
        names: list[str] = []
        section = OmegaConf.select(self._raw, "tool")
        if section is None:
            return names

        for name in section:
            names.append(str(name))

        return names


def _profiles() -> SandboxToolProfiles:
    loaded = SandboxToolProfiles.load()
    if loaded is None:
        pytest.skip("конфиг приложения недоступен: нет BOBA_CONFIG_PATH/BOBA_BASE")

    return loaded


def _networked() -> list[tuple[str, SandboxProfile]]:
    items: list[tuple[str, SandboxProfile]] = []
    for name, profile in _profiles().networked().items():
        items.append((name, profile))

    if not items:
        pytest.skip("в конфиге нет ни одного инструмента с network=true")

    return items


def _resolvable_host() -> str:
    """Хост из конфига, который резолвится снаружи; иначе проверять нечего."""
    for host in _profiles().http_hosts():
        try:
            socket.getaddrinfo(host, None)
        except socket.gaierror:
            continue

        return host

    pytest.skip("ни один хост из [web.*] не резолвится на самой машине")


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Профиль песочницы не зависит от сессии chainlit."""


class TestNetworkProfiles:
    """Сетевой профиль без резолвера — тихо сломанный инструмент."""

    LABEL: ClassVar[str] = "net:probe"
    PATH_VARS: ClassVar[dict[str, str]] = {"user_id": "0", "thread_id": "probe"}

    @classmethod
    def _run(cls, profile: SandboxProfile, command: str) -> str:
        if not has_bwrap(profile):
            pytest.skip("bwrap недоступен в доверенных каталогах профиля")

        runner = SandboxRunner(cls.LABEL, profile, lambda: cls.PATH_VARS)
        outcome = runner.run(command, "")

        assert outcome.succeeded, (
            f"{command}: rc={outcome.result.exit_code} "
            f"stdout={outcome.result.stdout!r} stderr={outcome.result.stderr!r}"
        )
        return outcome.result.stdout

    def test_network_profile_mounts_resolver(self) -> None:
        """resolv.conf и hosts обязаны быть в ro_binds сетевого профиля."""
        missing: list[str] = []
        for name, profile in _networked():
            targets: set[str] = set()
            for spec in profile.ro_binds:
                targets.add(spec.target)

            for required in ResolverFile:
                if required.value in targets:
                    continue

                missing.append(f"tool.{name}: {required.value}")

        assert missing == []

    def test_resolver_is_visible_inside(self) -> None:
        """Внутри песочницы виден host-резолвер, а не пустой файл из rootfs."""
        for _name, profile in _networked():
            resolver = self._run(profile, ProbeCommand.RESOLVER.render(""))

            assert "nameserver" in resolver

    def test_configured_host_resolves_inside(self) -> None:
        """Имя, которое резолвится на машине, обязано резолвиться и в песочнице."""
        host = _resolvable_host()

        for _name, profile in _networked():
            resolved = self._run(profile, ProbeCommand.LOOKUP.render(host))

            assert host in resolved
