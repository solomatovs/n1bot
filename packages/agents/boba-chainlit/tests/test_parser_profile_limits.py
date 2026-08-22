"""Профили payload-инструментов из конфига должны выдерживать реальный парсер.

Пороги с настоящей песочницы: RLIMIT_AS <= 2G и max_open_files = 10 роняют pdfium.
"""

from __future__ import annotations

import pytest

from boba.sandbox import SandboxProfile, SandboxToolConfig
from boba.settings import bind

MIN_ADDRESS_SPACE = 3 * 1024 * 1024 * 1024
MIN_OPEN_FILES = 64
MIN_PROCESSES = 16

RESOLVER_FILES = ("/etc/resolv.conf", "/etc/hosts")

_SECTIONS = [
    "tool.doc",
    "tool.ingest",
    "tool.web",
    "tool.confluence",
]

_NETWORK_SECTIONS = [
    *_SECTIONS,
    "tool.pg",
    "tool.kb",
]


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


def _bound(raw, sections) -> list[tuple[str, SandboxProfile]]:
    """Профиль запуска инструмента: он объявлен секцией [tool.<name>.sandbox]."""
    found: list[tuple[str, SandboxProfile]] = []
    for section in sections:
        sandbox = bind(raw, path=f"{section}.sandbox", model=SandboxToolConfig)
        found.append((section, sandbox.profile))
    return found


def _profiles(raw) -> list[tuple[str, SandboxProfile]]:
    return _bound(raw, _SECTIONS)


class TestParserProfileLimits:
    """Каждый инструмент с payload'ом должен получить рабочие лимиты."""

    def test_address_space_fits_pdfium(self, raw_config) -> None:
        for section, profile in _profiles(raw_config):
            if profile.limits.process_memory_bytes < MIN_ADDRESS_SPACE:
                raise AssertionError(
                    f"[{section}]: "
                    f"process_memory_bytes={profile.limits.process_memory_bytes} — "
                    "pdfium резервирует больше 2G адресного пространства и упадёт"
                )

    def test_enough_open_files(self, raw_config) -> None:
        for section, profile in _profiles(raw_config):
            if profile.limits.process_open_files < MIN_OPEN_FILES:
                raise AssertionError(
                    f"[{section}]: "
                    f"process_open_files={profile.limits.process_open_files} — "
                    "pdfium не откроет свою библиотеку (Too many open files)"
                )

    def test_enough_processes(self, raw_config) -> None:
        """Потолок задач секции опционален; заданный — не ниже нужного payload'у."""
        for section, profile in _profiles(raw_config):
            if profile.isolation.max_processes is None:
                continue

            if profile.isolation.max_processes < MIN_PROCESSES:
                raise AssertionError(
                    f"[{section}]: max_processes={profile.isolation.max_processes} — "
                    "payload запускает конвертеры отдельными процессами"
                )

    def test_network_matches_the_tool(self, raw_config) -> None:
        """Сеть — тем, кто сам ходит наружу: ingest тянет страницы и пишет в БД."""
        expected = {
            "tool.doc": False,
            "tool.ingest": True,
            "tool.web": True,
            "tool.confluence": True,
        }
        for section, profile in _profiles(raw_config):
            if profile.isolation.network is not expected[section]:
                wanted = expected[section]
                raise AssertionError(
                    f"[{section}]: network={profile.isolation.network}, "
                    f"ожидалось {wanted}"
                )

    def test_network_profiles_mount_resolver(self, raw_config) -> None:
        """Сеть без resolv.conf — это 'Temporary failure in name resolution'."""
        for section, profile in _bound(raw_config, _NETWORK_SECTIONS):
            if not profile.isolation.network:
                continue
            targets = set()
            for spec in profile.mounts.ro:
                targets.add(spec.target)
            missing = sorted(set(RESOLVER_FILES) - targets)
            if missing:
                raise AssertionError(
                    f"[{section}]: профиль с сетью не монтирует {missing} — "
                    "имена в песочнице не разрешатся"
                )
