from collections.abc import Mapping
from dataclasses import dataclass, field

from boba.toolkit.failure import ToolRefusalError

__all__ = [
    "AuthenticationError",
    "BaseError",
    "ExternalServiceError",
    "FailureReport",
    "HttpErrorMessage",
    "InternalServiceError",
    "RefusalError",
    "ServiceDisabledError",
    "UserInputError",
    "ViewErrorMessage",
]


@dataclass
class ViewErrorMessage:
    "Сообщение, которое увидит пользователь в чате"

    content: str
    author: str = "Error"
    fail_on_persist_error: bool = False


@dataclass
class HttpErrorMessage:
    "Http-ответ для браузера (например 401 + WWW-Authenticate: Negotiate для spnego)"

    status_code: int
    content: str
    headers: Mapping[str, str] = field(default_factory=dict)


class BaseError(Exception):
    "Базовый класс доменных ошибок; наследники задают view/history/http-представления"

    def view_message(self) -> ViewErrorMessage | None:
        "Возвращает соощение показываемое пользователю"
        return None

    def history_message(self) -> str | None:
        "Возвращает сообщение которое пишется в историю чата"
        return None

    def http_message(self) -> HttpErrorMessage | None:
        return None


class ExternalServiceError(BaseError):
    "Ошибка внешнего сервиса (postgres, ldap...): view/llm/log видят сообщение как есть"

    def __init__(self, service_name: str, message: str):
        super().__init__(message)
        self.message = message
        self.service_name = service_name
        self.status_code = 503

    def view_message(self) -> ViewErrorMessage | None:
        return ViewErrorMessage(content=self.message)

    def history_message(self) -> str | None:
        return self.message

    def http_message(self) -> HttpErrorMessage | None:
        return HttpErrorMessage(
            status_code=self.status_code,
            content=self.message,
        )


class ServiceDisabledError(BaseError):
    """Сервис выключен секцией конфига или не входит в процесс: ресурс api
    отвечает 503, чат и история видят причину как есть."""

    def __init__(self, section: str, message: str):
        super().__init__(message)
        self.section = section
        self.message = message
        self.status_code = 503

    def view_message(self) -> ViewErrorMessage | None:
        return ViewErrorMessage(content=self.message)

    def history_message(self) -> str | None:
        return self.message

    def http_message(self) -> HttpErrorMessage | None:
        return HttpErrorMessage(status_code=self.status_code, content=self.message)


class InternalServiceError(BaseError):
    "Внутренняя ошибка (наша вина): пользователю код для поддержки, детали в лог"

    def __init__(self, internal_detail: str, user_detail: str | None):
        super().__init__(internal_detail)
        self.internal_detail = internal_detail
        self.user_detail = user_detail
        self.status_code = 500

    def view_message(self) -> ViewErrorMessage | None:
        content = "Internal error"
        if self.user_detail:
            content += f"\n{self.user_detail}"

        return ViewErrorMessage(content)

    def http_message(self) -> HttpErrorMessage | None:
        return HttpErrorMessage(
            status_code=self.status_code,
            content=self.internal_detail,
        )


class UserInputError(BaseError):
    "Некорректные данные от пользователя: view видит сообщение, llm и лог — нет"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

    def view_message(self) -> ViewErrorMessage | None:
        return ViewErrorMessage(content=self.message)


class AuthenticationError(BaseError):
    "Не удалось аутентифицировать (Kerberos/LDAP/пароль): 401 в HTTP-слое, llm не видит"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message
        self.status_code = 401

    def view_message(self) -> ViewErrorMessage | None:
        return ViewErrorMessage(content=self.message)

    def history_message(self) -> str | None:
        return self.message

    def http_message(self) -> HttpErrorMessage | None:
        return HttpErrorMessage(
            status_code=self.status_code,
            content=self.message,
        )


class AuthorizationError(BaseError):
    "Аутентифицирован, но нет прав (403): view и llm видят сообщение как есть"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message
        self.status_code = 403

    def view_message(self) -> ViewErrorMessage | None:
        return ViewErrorMessage(content=self.message)

    def history_message(self) -> str | None:
        return self.message

    def http_message(self) -> HttpErrorMessage | None:
        return HttpErrorMessage(
            status_code=self.status_code,
            content=self.message,
        )


class ToolExecutionError(BaseError):
    "Инструмент агента упал: llm видит и может переиграть, пользователю не показываем"

    def __init__(self, tool_name: str, message: str):
        super().__init__(message)
        self.message = message
        self.tool_name = tool_name


class RateLimitError(ExternalServiceError):
    """Превышен лимит/квота внешнего провайдера (429), частный случай
    ExternalServiceError
    """


class AgentError(InternalServiceError):
    "Сломался сам граф/модель (не провайдер), частный случай InternalServiceError"


class RefusalError(ToolRefusalError):
    """Отказ операции приложения; текст готов для пользователя и LLM."""


@dataclass(frozen=True)
class FailureReport:
    """Разбор сбоя на три канала: журнал, чат и история LLM.

    Значение создаёт адаптер приложения FailureReports. Оно несёт выбранные
    представления ошибки, включая отсутствие сообщения для чата или истории.
    """

    log: str
    """Текст для журнала приложения; есть всегда. Трассу пишет сам журнал
    (logger.exception), поэтому её здесь нет."""

    view: str | None
    """Markdown для чата; None — пользователю показывать нечего."""

    history: str | None
    """Текст для истории LLM; None — модели об этом знать незачем."""
