"""Один секрет входа: chainlit берёт CHAINLIT_AUTH_SECRET из [session], и билет,
запечатанный этим секретом, открывается RuntimeConfig.sso_tickets()."""

from __future__ import annotations

import os
import time

import pytest
from omegaconf import DictConfig

from boba.aid.infra.entry import AppEntry, ChainlitEnv
from boba.kerberos import DelegationMode, SignInTicket
from boba.krb.seal import TicketSealer
from boba.runtime.config import RuntimeConfig


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Тест читает конфиг и печать: сессия чата ему не нужна."""


@pytest.mark.integration
class TestSharedAuthSecret:
    def test_env_secret_equals_session_secret(
        self,
        raw_config: DictConfig,
        runtime_config: RuntimeConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(ChainlitEnv.AUTH_SECRET, raising=False)

        AppEntry().export_env(raw_config)

        assert os.environ[ChainlitEnv.AUTH_SECRET] == runtime_config.session.auth_secret

    def test_ticket_sealed_by_chainlit_opens_in_runtime(
        self,
        raw_config: DictConfig,
        runtime_config: RuntimeConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(ChainlitEnv.AUTH_SECRET, raising=False)
        AppEntry().export_env(raw_config)

        tickets = runtime_config.sso_tickets()
        assert tickets is not None, "[auth] kerberos is not configured on the stand"

        ticket = SignInTicket(
            principal="user@EXAMPLE.COM",
            mode=DelegationMode.FORWARDED,
            ccache=b"ccache-bytes",
            expires_at=int(time.time()) + 600,
        )
        sealed = TicketSealer(os.environ[ChainlitEnv.AUTH_SECRET]).seal(ticket)

        assert tickets.open(sealed) == ticket
