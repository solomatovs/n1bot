"""Журнал обязателен: без тома приложение не стартует, флага отключения нет.

Проверяется стартовый путь приложения целиком: секция конфига, создание тома
и постановка журнала в StreamJournalHub — тот самый вызов, который делает
bootstrap.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from boba.chainlit.infra import providers
from boba.chainlit.infra.config import StreamJournalConfig
from boba.sandbox.journal import JournalError, StreamJournalHub
from boba.sandbox.runner import ToolCallContext
from boba.toolkit.channels import Channel

VAULT_DIR = "logs"


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Старт приложения идёт до сессии: контекст chainlit тесту не нужен."""


@pytest.fixture(autouse=True)
def journal_hub() -> Iterator[None]:
    """Журнал приложения глобален: тест возвращает хаб в исходное состояние."""
    yield

    StreamJournalHub.reset()


class TestJournalIsMandatory:
    """Секция [stream_journal] обязательна, и её том проверяется на старте."""

    def test_section_without_dir_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            StreamJournalConfig.model_validate({})

    def test_the_enable_flag_is_gone(self) -> None:
        """«Журнала нет» в рантайме не бывает: старый флаг ничего не выключает."""
        assert "enable" not in StreamJournalConfig.model_fields

        cfg = StreamJournalConfig.model_validate(
            {"dir": "/var/lib/boba/tool-logs", "enable": False}
        )

        assert cfg.dir == "/var/lib/boba/tool-logs"

    def test_unwritable_vault_stops_the_start(self, tmp_path: Path) -> None:
        blocked = tmp_path / "blocked"
        blocked.mkdir()
        os.chmod(blocked, 0o500)

        cfg = StreamJournalConfig.model_validate({"dir": str(blocked / VAULT_DIR)})

        try:
            with pytest.raises(JournalError):
                providers.stream_journal(cfg)
        finally:
            os.chmod(blocked, 0o700)

    def test_started_journal_serves_the_application(self, tmp_path: Path) -> None:
        """Том создан, журнал в хабе, и записанный канал лежит под его корнем."""
        root = tmp_path / VAULT_DIR
        cfg = StreamJournalConfig.model_validate(
            {"dir": str(root), "reserve_bytes": 0}
        )

        journal = providers.stream_journal(cfg)

        assert root.is_dir()
        assert StreamJournalHub.get() is journal

        context = ToolCallContext(
            user_id="7", thread_id="t-1", call_id="c-1", tool="bash"
        )
        call = journal.open(context)
        sink = call.sink("bash", Channel.TOOL_PAYLOAD, None)
        sink.feed("итог стадии\n".encode())
        sink.close()
        call.close("")

        written = root / "7" / "t-1" / "c-1.bash.tool_payload.log"

        assert written.read_bytes() == "итог стадии\n".encode()
