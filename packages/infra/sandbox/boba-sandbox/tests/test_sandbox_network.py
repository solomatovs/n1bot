"""Сеть песочницы: профиль с network=true обязан резолвить имена.

Профили берутся из конфига сервиса boba-mcp (фикстура raw_config набора) тем
же вызовом, что и загрузчик плагинов, — проверяется ровно то окружение, в
котором инструмент ходит в сеть.

Ошибка, ради которой написан тест: rootfs несёт собственный пустой
/etc/resolv.conf, и если host-файл не примонтирован поверх, getaddrinfo внутри
отвечает EAI_AGAIN («Temporary failure in name resolution») при живой сети —
именно так падал confluence_search, пока kb_fts_search ходил в базу по /etc/hosts.
"""

from __future__ import annotations

import socket
from enum import StrEnum
from typing import ClassVar

import pytest
from omegaconf import DictConfig, OmegaConf

from boba.runtime.plugins import EntryPointPlugins
from boba.sandbox.profile import SandboxProfile
from boba.sandbox.runner import has_bwrap
from boba.stand.sandbox import section_profile
from boba.stand.shell import ShellRun
from boba.stand.zygote import ZygoteStand


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
    """Профили инструментов из конфига сервиса: имя инструмента -> профиль."""

    def __init__(self, raw: DictConfig) -> None:
        self._raw = raw

    def networked(self) -> dict[str, SandboxProfile]:
        """Инструменты, которым конфиг разрешил сеть."""
        installed = EntryPointPlugins().discover()

        profiles: dict[str, SandboxProfile] = {}
        for name in self._tool_names():
            if name not in installed:
                continue

            section = OmegaConf.select(self._raw, f"tool.{name}.sandbox")
            if section is None:
                continue

            effective = section_profile(self._raw, name)
            if not effective.isolation.network:
                continue

            profiles[name] = effective

        return profiles

    def http_hosts(self) -> list[str]:
        """Хосты сервисных профилей плагинов: по ним инструменты и ходят."""
        hosts: list[str] = []
        host = OmegaConf.select(self._raw, "tool.ingest.confluence.host")
        if host:
            hosts.append(str(host))

        return hosts

    def _tool_names(self) -> list[str]:
        names: list[str] = []
        section = OmegaConf.select(self._raw, "tool")
        if section is None:
            return names

        for name in section:
            names.append(str(name))

        return names


def _networked(raw_config: DictConfig) -> list[tuple[str, SandboxProfile]]:
    items: list[tuple[str, SandboxProfile]] = []
    for name, profile in SandboxToolProfiles(raw_config).networked().items():
        items.append((name, profile))

    if not items:
        pytest.fail("в конфиге нет ни одного инструмента с network=true")

    return items


def _resolvable_host(raw_config: DictConfig) -> str:
    """Хост из конфига, который резолвится снаружи; без него тест падает."""
    for host in SandboxToolProfiles(raw_config).http_hosts():
        try:
            socket.getaddrinfo(host, None)
        except socket.gaierror:
            continue

        return host

    pytest.fail("ни один сервисный хост конфига не резолвится на самой машине")


class TestNetworkProfiles:
    """Сетевой профиль без резолвера — тихо сломанный инструмент."""

    LABEL: ClassVar[str] = "net:probe"
    PATH_VARS: ClassVar[dict[str, str]] = {"user_id": "0", "thread_id": "probe"}

    @classmethod
    def _run(
        cls, zygote_stand: ZygoteStand, profile: SandboxProfile, command: str
    ) -> str:
        """Команда тем же путём, что в проде: зигота секции и её исполнитель."""
        if not has_bwrap(profile):
            pytest.fail("bwrap недоступен в доверенных каталогах профиля")

        caller = zygote_stand.caller(
            cls.LABEL, profile, path_vars=lambda: cls.PATH_VARS
        )
        try:
            outcome = ShellRun.call_text(caller, command)
        finally:
            zygote_stand.stop()

        if outcome.exit_code != 0:
            raise AssertionError(
                f"{command}: rc={outcome.exit_code} "
                f"stdout={outcome.stdout!r} stderr={outcome.stderr!r}"
            )
        return outcome.stdout

    def test_network_profile_mounts_resolver(self, raw_config: DictConfig) -> None:
        """resolv.conf и hosts обязаны быть в ro_binds сетевого профиля."""
        missing: list[str] = []
        for name, profile in _networked(raw_config):
            targets: set[str] = set()
            for spec in profile.mounts.ro:
                targets.add(spec.target)

            for required in ResolverFile:
                if required.value in targets:
                    continue

                missing.append(f"tool.{name}: {required.value}")

        if missing != []:
            raise AssertionError("missing == []")

    def test_resolver_is_visible_inside(
        self, zygote_stand: ZygoteStand, raw_config: DictConfig
    ) -> None:
        """Внутри песочницы виден host-резолвер, а не пустой файл из rootfs."""
        for _name, profile in _networked(raw_config):
            resolver = self._run(
                zygote_stand, profile, ProbeCommand.RESOLVER.render("")
            )

            if "nameserver" not in resolver:
                raise AssertionError('"nameserver" in resolver')

    def test_configured_host_resolves_inside(
        self, zygote_stand: ZygoteStand, raw_config: DictConfig
    ) -> None:
        """Имя, которое резолвится на машине, обязано резолвиться и в песочнице."""
        host = _resolvable_host(raw_config)

        for _name, profile in _networked(raw_config):
            resolved = self._run(
                zygote_stand, profile, ProbeCommand.LOOKUP.render(host)
            )

            if host not in resolved:
                raise AssertionError("host in resolved")
