"""
Модели ответов api_server hermes:
    session
    message
"""

from dataclasses import Field, dataclass, field, fields
from enum import StrEnum
from typing import Any, ClassVar, Self

__all__ = [
    "HermesError",
    "HermesMessage",
    "HermesRole",
    "HermesSession",
    "HermesSessionPage",
    "HermesToolCall",
    "HermesToolFunction",
    "Payload",
]


class HermesRole(StrEnum):
    """Роль сообщения в истории hermes."""

    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    SYSTEM = "system"
    DEVELOPER = "developer"

    @classmethod
    def of(cls, value: Any) -> "HermesRole | None":
        """Роль или None, если hermes прислал незнакомую."""
        try:
            return cls(str(value))
        except ValueError:
            return None


class Payload:
    """База моделей ответа: собирает поля из json, лишние ключи отбрасывает."""

    __slots__ = ()
    # сам Payload не dataclass, но наследники им становятся: без объявления
    # fields(cls) не проходит проверку типов
    __dataclass_fields__: ClassVar[dict[str, Field[Any]]]

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> Self:
        """Модель из тела ответа; неизвестные поля hermes игнорируются."""
        return cls._known_of(payload)

    @classmethod
    def _known_of(cls, payload: dict[str, Any]) -> Self:
        """Отбор известных полей.

        Отдельным методом, потому что slots=True пересоздаёт класс и zero-arg
        super() в наследниках после этого не работает.
        """
        known = {f.name for f in fields(cls)}

        return cls(**{k: v for k, v in payload.items() if k in known})


@dataclass(slots=True)
class HermesSession(Payload):
    """Сессия hermes — то, что в chainlit показывается thread'ом

    Поля повторяют `_session_response` api_server.
    Состав ответа зависит от запроса: `preview` и `last_active`
    приходят только в списке сессий, у
    одиночной их нет, поэтому обязателен только id.
    """

    id: str
    title: str | None = None
    preview: str | None = None
    source: str | None = None
    model: str | None = None
    user_id: str | None = None
    started_at: float | None = None
    ended_at: float | None = None
    end_reason: str | None = None
    last_active: float | None = None
    message_count: int | None = None
    tool_call_count: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    estimated_cost_usd: float | None = None
    actual_cost_usd: float | None = None
    api_call_count: int | None = None
    parent_session_id: str | None = None
    has_system_prompt: bool = False
    has_model_config: bool = False


@dataclass(slots=True)
class HermesToolFunction(Payload):
    """Что вызывает инструмент: имя и аргументы json-строкой."""

    name: str = ""
    arguments: str = ""


@dataclass(slots=True)
class HermesToolCall(Payload):
    """Запрос вызова инструмента внутри сообщения ассистента.

    call_id и response_item_id в живой выдаче повторяют id: они нужны hermes
    для сшивки с ответом провайдера. Ответ инструмента ссылается на id через
    своё поле tool_call_id.
    """

    id: str
    type: str | None = None
    call_id: str | None = None
    response_item_id: str | None = None
    function: HermesToolFunction = field(default_factory=HermesToolFunction)

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> Self:
        """Помимо общего разбора собирает вложенную function."""
        call = cls._known_of({k: v for k, v in payload.items() if k != "function"})
        function = payload.get("function")
        if isinstance(function, dict):
            call.function = HermesToolFunction.from_api(function)

        return call


@dataclass(slots=True)
class HermesSessionPage(Payload):
    """Страница списка сессий: сами сессии и признак продолжения."""

    data: list[HermesSession] = field(default_factory=list)
    limit: int = 0
    offset: int = 0
    has_more: bool = False

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> Self:
        """Помимо общего разбора собирает вложенные сессии."""
        page = cls._known_of({k: v for k, v in payload.items() if k != "data"})
        page.data = [
            HermesSession.from_api(row)
            for row in payload.get("data") or []
            if isinstance(row, dict)
        ]
        return page


@dataclass(slots=True)
class HermesError(Payload):
    """Тело ошибки api_server: {"error": {...}}.

    Ключ param в ответах присутствует не всегда — у 401 его нет.
    """

    message: str = ""
    type: str = ""
    code: str | None = None
    param: str | None = None

    @classmethod
    def of(cls, payload: Any) -> "HermesError | None":
        """Ошибка из тела ответа; None, если конверта нет."""
        if not isinstance(payload, dict):
            return None

        error = payload.get("error")
        if not isinstance(error, dict):
            return None

        return cls.from_api(error)


@dataclass(slots=True)
class HermesMessage(Payload):
    """Сообщение истории — то, что в chainlit показывается шагом.

    Поля повторяют `_message_response` api_server, обязательность — колонки
    таблицы messages: id, session_id, role и timestamp объявлены NOT NULL,
    остальное заполняется по ходу. Ответ всегда содержит все ключи (запрос
    идёт по SELECT *), поэтому отсутствие обязательного поля означает, что
    api_server разъехался с моделью, и падать здесь правильно.
    """

    id: int
    session_id: str
    role: str
    timestamp: float
    # у ассистента с вызовом инструмента текста нет, а мультимодальное
    # сообщение приходит списком частей
    content: str | list[Any] | dict[str, Any] | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_calls: list[HermesToolCall] = field(default_factory=list)
    token_count: int | None = None
    finish_reason: str | None = None
    reasoning: str | None = None
    reasoning_content: str | None = None

    def step_name(self):
        return self.tool_name or self.role

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> Self:
        """Помимо общего разбора собирает вложенные tool_calls."""
        message = cls._known_of(payload)
        message.tool_calls = [
            HermesToolCall.from_api(call)
            for call in payload.get("tool_calls") or []
            if isinstance(call, dict) and call.get("id")
        ]
        return message

    def role_of(self) -> HermesRole | None:
        """Роль сообщения; None для незнакомой."""
        return HermesRole.of(self.role)
