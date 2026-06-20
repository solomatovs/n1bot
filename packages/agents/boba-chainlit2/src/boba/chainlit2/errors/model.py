import random
import string
from dataclasses import dataclass, field
from logging import Logger


@dataclass
class ViewErrorMessage:
    "Сообщение которое увидит пользователь в chainlit ui"

    content: str
    author: str = "Error"
    fail_on_persist_error: bool = False


@dataclass
class HttpErrorMessage:
    """
    Http сообщение для браузера.
    Полезно для spnego, когда нужно отправить ответ в виде:
        401 + WWW-Authenticate: Negotiate + пустое тело
        что заставит браузер сформировать spnego token и повторить запрос
        без участия пользователя в этом процессе
    """

    status_code: int
    content: str
    headers: list[tuple[bytes, bytes]] = field(default_factory=list)


class BaseError(Exception):
    """
    базовый класс ошибок в приложении
    Если хочешь, что бы ошибка корректно отобразилась
    наследуйся от этого класса и определяй базовые методы
    """

    # это http code, по умолчанию он будет 500
    # в классах наследниках переопределяется
    status_code: int = 500

    def view_message(self) -> ViewErrorMessage | None:
        "Возвращает соощение показываемое пользователю"
        return None

    def log_message(self, logger: Logger):
        "Возращает сообщение отправляемое в logger"

    def history_message(self) -> str | None:
        """
        Возвращает сообщение которое пишется в историю чата
        """
        return None

    def http_message(self) -> HttpErrorMessage:
        content = ""
        if m := self.view_message():
            content = m.content

        return HttpErrorMessage(
            status_code=self.status_code,
            content=content,
        )

class ExternalServiceError(BaseError):
    """
    Ошибка во внешнем сервисе (postgres, clickhouse, ldap, kerberos, etc...)
    Не наша вина.
    - view: показываем сообщение пользователю как есть
    - llm: показываем llm в историю такое же сообщение
    - log: записываем в log сообщение как есть
    """

    def __init__(self, service_name: str, message: str):
        super().__init__(message)
        self.message = message
        self.service_name = service_name
        self.status_code = 503

    def view_message(self) -> ViewErrorMessage | None:
        return ViewErrorMessage(content=self.message)

    def history_message(self) -> str | None:
        return self.message

    def log_message(self, logger: Logger):
        logger.error("%s: %s", self.service_name, self.message, exc_info=self)


class InternalServiceError(BaseError):
    """
    Внутренняя ошибка (Например некорректная конфигурация)
    Наша вина, детали сообщения пользователю не показываем
    - view: показываем пользователю минимальное сообщение и уникальный код ошибки, который он может передать в поддержку
    - llm: не видит это сообщение
    - log: этот же код пишется в лог с максимально подробным сообщением, что бы программист смог разобраться в проблеме
    Сообщаем пользователю код, который он может отправить в поддержку
    Программист в логах сможет увидеть больше информации
    """  # noqa: E501

    def __init__(self, internal_detail: str, user_detail: str | None):
        super().__init__(internal_detail)
        # тех.описание только для лога, пользователь не видит
        self.internal_detail = internal_detail
        self.user_detail = user_detail
        self.code = self.generate_code()
        self.status_code = 500

    @staticmethod
    def generate_code():
        "генератор рандомных кодов сообщений"
        code = "".join(
            random.choices(  # noqa: S311
                string.ascii_uppercase + string.digits,
                k=4,
            )
        )
        return code

    def view_message(self) -> ViewErrorMessage | None:
        content = (
            f"Internal error, please report this error code to support: {self.code}"
        )
        if self.user_detail:
            content += f"\n{self.user_detail}"

        return ViewErrorMessage(content)

    def log_message(self, logger: Logger):
        logger.error("code [%s]: %s", self.code, str(self.internal_detail))

class UserInputError(BaseError):
    """
    Пришли некорректные данные от пользователя (Например ошибка валидации по json-схеме)
    - view: показываем сообщение как есть
    - llm: не видит сообщение, ей это не нужно
    - log: сообщение не нужно, нет смысла логировать
    """

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message
        self.status_code = 400

    def view_message(self) -> ViewErrorMessage | None:
        return ViewErrorMessage(content=self.message)


class AuthenticationError(BaseError):
    """
    Не удалось аутентифицировать пользователя (Kerberos/LDAP/пароль)
    Возникает в HTTP-слое auth-callback (отдаётся как 401), а не в чате
    - view: показываем сообщение как есть
    - llm: не видит, до агента дело не дошло
    - log: warning без traceback, это ожидаемая ситуация
    """

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message
        self.status_code = 401

    def view_message(self) -> ViewErrorMessage | None:
        return ViewErrorMessage(content=self.message)

    def log_message(self, logger: Logger):
        logger.warning("%s: %s", type(self).__name__, self.message)


class AuthorizationError(BaseError):
    """
    Аутентифицирован, но нет прав (нет нужной группы / доступа к инструменту)
    - view: показываем сообщение как есть
    - llm: видит — должен узнать, что инструмент/данные недоступны
    - log: warning без traceback
    """

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message
        self.status_code = 403

    def view_message(self) -> ViewErrorMessage | None:
        return ViewErrorMessage(content=self.message)

    def history_message(self) -> str | None:
        return self.message

    def log_message(self, logger: Logger):
        logger.warning("%s: %s", type(self).__name__, self.message)


class ToolExecutionError(BaseError):
    """
    Инструмент агента упал во время выполнения
    - view: пользователю не показываем, это внутренняя механика агента
    - llm: видит — должен переиграть (повторить / выбрать другой инструмент)
    - log: warning с traceback
    """

    def __init__(self, tool_name: str, message: str):
        super().__init__(message)
        self.message = message
        self.tool_name = tool_name
        self.status_code = 503

    def history_message(self) -> str | None:
        return self.message

    def log_message(self, logger: Logger):
        logger.warning("%s: %s", type(self).__name__, self.message, exc_info=self)


class RateLimitError(ExternalServiceError):
    """
    Превышен лимит запросов / квота внешнего провайдера (429)
    Частный случай ExternalServiceError: каналы те же (view/llm/log как есть)
    """

    status_code = 500


class AgentError(InternalServiceError):
    """
    Сломался сам граф/модель (не провайдер) — наша вина
    Частный случай InternalServiceError: пользователю код, llm не видит, детали в лог
    """

    status_code = 500

