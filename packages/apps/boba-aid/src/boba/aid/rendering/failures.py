"""Представление ошибок приложения для журнала, чата, истории и HTTP.

Ошибки: своих не выпускает.
"""

from boba.core.errors import PublicError
from boba.identity.errors import BaseError, FailureReport, InternalServiceError
from boba.toolkit.failure import FailurePacker

__all__ = ["FailureReports"]


class FailureReports:
    """Адаптирует доменные ошибки для границ приложения через FailurePacker.

    Создаётся HTTP-middleware и обработчиками чата; сохраняет решения домена
    о видимости ошибки пользователю и модели.
    """

    def __init__(self) -> None:
        self._failures = FailurePacker()

    def describe(self, error: BaseException) -> FailureReport:
        failure = self._failures.pack(error)
        described = failure.llm_view()

        if isinstance(error, PublicError):
            return FailureReport(
                log=error.diagnostic, view=str(error), history=str(error)
            )

        if not isinstance(error, BaseError):
            return FailureReport(
                log=described, view=failure.markdown(), history=described
            )

        view = None
        if message := error.view_message():
            view = message.content

        return FailureReport(log=described, view=view, history=error.history_message())

    def domain(self, error: Exception) -> BaseError:
        if isinstance(error, BaseError):
            return error

        described = self.describe(error)
        wrapped = InternalServiceError(internal_detail=described.log, user_detail=None)
        wrapped.__cause__ = error

        return wrapped
