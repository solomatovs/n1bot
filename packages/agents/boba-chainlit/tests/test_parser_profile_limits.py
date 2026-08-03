"""Профили payload-инструментов из конфига должны выдерживать реальный парсер.

Пороги с настоящей песочницы: RLIMIT_AS <= 2G и max_open_files = 10 роняют pdfium.
"""

from __future__ import annotations

import pytest

from boba.settings import bind
from boba.tool.doc import DocToolsConfig
from boba.tool.kb import PostgresKnowledgeBaseConfig
from boba.tool.kb.confluence import ConfluenceToolsConfig
from boba.tool.kb.confluence.ingest_base import ConfluenceIngestConfig
from boba.tool.pg import SqlExecutorConfig
from boba.tool.web import WebGrepConfig
from boba.toolkit.sandbox import SandboxProfile

MIN_ADDRESS_SPACE = 3 * 1024 * 1024 * 1024
MIN_OPEN_FILES = 64
MIN_PROCESSES = 16

RESOLVER_FILES = ("/etc/resolv.conf", "/etc/hosts")

_SECTIONS = [
    ("tool.doc", DocToolsConfig),
    ("tool.ingest", ConfluenceIngestConfig),
    ("tool.web", WebGrepConfig),
    ("tool.confluence", ConfluenceToolsConfig),
]

_NETWORK_SECTIONS = [
    *_SECTIONS,
    ("tool.pg", SqlExecutorConfig),
    ("tool.kb", PostgresKnowledgeBaseConfig),
]


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


def _bound(raw, sections) -> list[tuple[str, SandboxProfile]]:
    found: list[tuple[str, SandboxProfile]] = []
    for section, model in sections:
        cfg = bind(raw, path=section, model=model)
        found.append((section, cfg.sandbox.effective()))
    return found


def _profiles(raw) -> list[tuple[str, SandboxProfile]]:
    return _bound(raw, _SECTIONS)


class TestParserProfileLimits:
    """Каждый инструмент с payload'ом должен получить рабочие лимиты."""

    def test_address_space_fits_pdfium(self, raw_config) -> None:
        for section, profile in _profiles(raw_config):
            assert profile.max_memory_bytes >= MIN_ADDRESS_SPACE, (
                f"[{section}]: max_memory_bytes={profile.max_memory_bytes} — "
                "pdfium резервирует больше 2G адресного пространства и упадёт"
            )

    def test_enough_open_files(self, raw_config) -> None:
        for section, profile in _profiles(raw_config):
            assert profile.max_open_files >= MIN_OPEN_FILES, (
                f"[{section}]: max_open_files={profile.max_open_files} — "
                "pdfium не откроет свою библиотеку (Too many open files)"
            )

    def test_enough_processes(self, raw_config) -> None:
        for section, profile in _profiles(raw_config):
            assert profile.max_processes >= MIN_PROCESSES, (
                f"[{section}]: max_processes={profile.max_processes} — "
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
            assert profile.network is expected[section], (
                f"[{section}]: network={profile.network}, "
                f"ожидалось {expected[section]}"
            )

    def test_network_profiles_mount_resolver(self, raw_config) -> None:
        """Сеть без resolv.conf — это 'Temporary failure in name resolution'."""
        for section, profile in _bound(raw_config, _NETWORK_SECTIONS):
            if not profile.network:
                continue
            targets = set()
            for spec in profile.ro_binds:
                targets.add(spec.target)
            missing = sorted(set(RESOLVER_FILES) - targets)
            assert not missing, (
                f"[{section}]: профиль с сетью не монтирует {missing} — "
                "имена в песочнице не разрешатся"
            )
