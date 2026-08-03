"""Профили payload-инструментов должны выдерживать реальный парсер.

Проверка идёт по конфигу приложения, не по выдуманному профилю: заниженный
лимит виден до того, как пользователь загрузит документ и получит невнятную
панику pdfium из глубины rust-биндингов.

Пороги измерены на настоящей песочнице (см. test_payload_in_sandbox):
- RLIMIT_AS <= 2G      -> pdfium не грузится ("failed to map segment");
- max_open_files = 10  -> pdfium не грузится ("Too many open files"),
  дескрипторы тратят python, сам pdfium и канал fuse к образу workspace.
"""

from __future__ import annotations

import pytest

from boba.chainlit2.agent.tools.confluence import ConfluenceToolsConfig
from boba.chainlit2.agent.tools.confluence.ingest_base import ConfluenceIngestConfig
from boba.chainlit2.agent.tools.doc import DocToolsConfig
from boba.chainlit2.agent.tools.web import WebGrepConfig
from boba.chainlit2.sandbox import SandboxProfile
from boba.settings import bind

MIN_ADDRESS_SPACE = 3 * 1024 * 1024 * 1024
MIN_OPEN_FILES = 64
MIN_PROCESSES = 16

_SECTIONS = [
    ("tool.doc", DocToolsConfig),
    ("tool.ingest", ConfluenceIngestConfig),
    ("tool.web", WebGrepConfig),
    ("tool.confluence", ConfluenceToolsConfig),
]


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


def _profiles(raw) -> list[tuple[str, SandboxProfile]]:
    found: list[tuple[str, SandboxProfile]] = []
    for section, model in _SECTIONS:
        cfg = bind(raw, path=section, model=model)
        found.append((section, cfg.sandbox.effective()))
    return found


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
