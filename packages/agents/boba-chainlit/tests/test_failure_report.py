"""Разбор сбоя на три канала: журнал, чат, история LLM.

Контракт простой: чтобы сообщить о сбое, достаточно выбросить исключение —
дальше все три канала получают формулировку из одного места. Доменная ошибка
меняет это своими представлениями, обычная идёт как есть.
"""

from __future__ import annotations

import pytest

from boba.chainlit.domain.errors import (
    AuthenticationError,
    ExternalServiceError,
    FailureReport,
    InternalServiceError,
    UserInputError,
)


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


class TestPlainException:
    """Обычное исключение: три канала видят одно и то же с причинами."""

    @staticmethod
    def _connection_failure() -> Exception:
        try:
            try:
                raise OSError("All connection attempts failed")
            except OSError as low:
                raise RuntimeError("Connection error.") from low
        except RuntimeError as error:
            return error

    def test_all_three_channels_match(self) -> None:
        report = FailureReport.of(self._connection_failure())

        assert report.log == report.view == report.history
        assert "RuntimeError: Connection error." in report.log
        assert "OSError: All connection attempts failed" in report.log


class TestDomainErrors:
    """Доменная ошибка сама решает, что показать и что скрыть."""

    def test_external_service_reaches_user_and_model(self) -> None:
        """Сбой внешнего сервиса объясняет ход и пользователю, и модели."""
        report = FailureReport.of(ExternalServiceError("postgres", "база недоступна"))

        assert report.view == "база недоступна"
        assert report.history == "база недоступна"
        assert "ExternalServiceError" in report.log

    def test_internal_error_hides_details_from_user(self) -> None:
        """Своя авария: пользователю общая формулировка, детали — в журнал."""
        report = FailureReport.of(
            InternalServiceError(internal_detail="pool exhausted", user_detail=None)
        )

        assert report.view == "Internal error"
        assert "pool exhausted" in report.log

    def test_user_input_error_stays_out_of_history(self) -> None:
        """Ошибка ввода — разговор с пользователем, модели она не нужна."""
        report = FailureReport.of(UserInputError("файл не поддерживается"))

        assert report.view == "файл не поддерживается"
        assert report.history is None

    def test_authentication_error_keeps_its_message(self) -> None:
        """Текущее поведение класса: сообщение идёт и в чат, и в историю.

        Докстринг AuthenticationError обещает «llm не видит», а
        history_message отдаёт текст — расхождение живёт в самом классе и
        правится там же, не в разборе.
        """
        report = FailureReport.of(AuthenticationError("нет билета"))

        assert report.view == "нет билета"
        assert report.history == "нет билета"
        assert "нет билета" in report.log
