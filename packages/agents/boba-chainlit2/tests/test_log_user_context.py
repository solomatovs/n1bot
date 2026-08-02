"""Пользователь в каждой строке лога: подставляет фабрика LogRecord."""

from __future__ import annotations

import logging

import pytest

from boba.chainlit2.infra import log_context
from boba.chainlit2.infra.log_context import UserLogContext


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


@pytest.fixture(autouse=True)
def installed() -> None:
    UserLogContext.install()


class TestUserInEveryRecord:
    @staticmethod
    def _record() -> logging.LogRecord:
        return logging.getLogger("probe").makeRecord(
            "probe", logging.INFO, "f.py", 1, "сообщение", (), None
        )

    def test_attribute_present_without_session(self) -> None:
        record = self._record()
        assert getattr(record, UserLogContext.ATTRIBUTE) == UserLogContext.UNKNOWN

    def test_user_label_taken_from_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(log_context, "current_user_label", lambda: "ivanov")
        assert getattr(self._record(), UserLogContext.ATTRIBUTE) == "ivanov"

    def test_broken_session_does_not_break_logging(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom() -> str:
            msg = "нет контекста"
            raise RuntimeError(msg)

        monkeypatch.setattr(log_context, "current_user_label", boom)
        assert getattr(self._record(), UserLogContext.ATTRIBUTE) == (
            UserLogContext.UNKNOWN
        )

    def test_install_is_idempotent(self) -> None:
        factory = logging.getLogRecordFactory()
        UserLogContext.install()
        UserLogContext.install()
        assert logging.getLogRecordFactory() is factory

    def test_format_with_user_field(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(log_context, "current_user_label", lambda: "petrov")
        formatter = logging.Formatter("[%(user)s] %(message)s")
        assert formatter.format(self._record()) == "[petrov] сообщение"
