"""Разбор сбоя на три канала: журнал, чат, история LLM.

Контракт простой: чтобы сообщить о сбое, достаточно выбросить исключение —
дальше все три канала получают формулировку из одного места. Доменная ошибка
меняет это своими представлениями, обычная идёт как есть.
"""

from __future__ import annotations

import pytest

from boba.identity.errors import (
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

        if not (report.log == report.view == report.history):
            raise AssertionError("report.log == report.view == report.history")
        if "RuntimeError: Connection error." not in report.log:
            raise AssertionError('"RuntimeError: Connection error." in report.log')
        if "OSError: All connection attempts failed" not in report.log:
            raise AssertionError('"OSError: All connection attempts failed" in report…')


class TestDomainErrors:
    """Доменная ошибка сама решает, что показать и что скрыть."""

    def test_external_service_reaches_user_and_model(self) -> None:
        """Сбой внешнего сервиса объясняет ход и пользователю, и модели."""
        report = FailureReport.of(ExternalServiceError("postgres", "база недоступна"))

        if report.view != "база недоступна":
            raise AssertionError('report.view == "база недоступна"')
        if report.history != "база недоступна":
            raise AssertionError('report.history == "база недоступна"')
        if "ExternalServiceError" not in report.log:
            raise AssertionError('"ExternalServiceError" in report.log')

    def test_internal_error_hides_details_from_user(self) -> None:
        """Своя авария: пользователю общая формулировка, детали — в журнал."""
        report = FailureReport.of(
            InternalServiceError(internal_detail="pool exhausted", user_detail=None)
        )

        if report.view != "Internal error":
            raise AssertionError('report.view == "Internal error"')
        if "pool exhausted" not in report.log:
            raise AssertionError('"pool exhausted" in report.log')

    def test_user_input_error_stays_out_of_history(self) -> None:
        """Ошибка ввода — разговор с пользователем, модели она не нужна."""
        report = FailureReport.of(UserInputError("файл не поддерживается"))

        if report.view != "файл не поддерживается":
            raise AssertionError('report.view == "файл не поддерживается"')
        if report.history is not None:
            raise AssertionError("report.history is None")

    def test_authentication_error_keeps_its_message(self) -> None:
        """Текущее поведение класса: сообщение идёт и в чат, и в историю.

        Докстринг AuthenticationError обещает «llm не видит», а
        history_message отдаёт текст — расхождение живёт в самом классе и
        правится там же, не в разборе.
        """
        report = FailureReport.of(AuthenticationError("нет билета"))

        if report.view != "нет билета":
            raise AssertionError('report.view == "нет билета"')
        if report.history != "нет билета":
            raise AssertionError('report.history == "нет билета"')
        if "нет билета" not in report.log:
            raise AssertionError('"нет билета" in report.log')
