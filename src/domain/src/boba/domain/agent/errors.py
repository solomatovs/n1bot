"""Agent-специфичные доменные ошибки + практический гайд по всей иерархии.

Этот модуль держит LLM-иерархию (:class:`LLMError` + подклассы) и
:class:`ToolFeedbackError`. Базовые маркеры (``RoutableError``,
``TerminalError``, ``UserFeedbackError``, ``LLMFeedbackError``,
``Retryable``, ``UserNoticeError``) живут в
:mod:`boba.domain.core.errors` — их используют и core-сервисы
(``agent.prompt``, ``agent.history``), и agent-слой.


════════════════════════════════════════════════════════════════════
  Полная иерархия
════════════════════════════════════════════════════════════════════

::

    RoutableError                                          [core.errors]
    │
    ├── UserFeedbackError           user видит, LLM не видит
    │   │
    │   ├── TerminalError              + цикл стоп
    │   │   │
    │   │   ├── LLMError            [agent.errors]   → GenerationFailed
    │   │   │   ├── RetryableLLMError  (+ Retryable миксин)
    │   │   │   │   ├── LLMConnectionError
    │   │   │   │   ├── LLMTimeoutError
    │   │   │   │   ├── LLMRateLimitError
    │   │   │   │   └── LLMProviderInternalError
    │   │   │   └── PermanentLLMError
    │   │   │       ├── LLMAuthError
    │   │   │       ├── LLMInvalidRequestError
    │   │   │       └── LLMContextLengthError
    │   │   │
    │   │   ├── PromptError         [agent.prompt]     → PromptFailed
    │   │   │   ├── RetryablePromptError (+ Retryable)
    │   │   │   └── PermanentPromptError
    │   │   │       └── PromptProviderError
    │   │   │
    │   │   ├── MessageStoreError   [agent.messages]  → PersistenceFailed
    │   │   │   ├── MessageStoreWriteError
    │   │   │   └── MessageStoreReadError
    │   │   │
    │   │   ├── MaxIterationsExceededError [agent.errors] → MaxIterationsReached
    │   │   │
    │   │   └── RepeatedFormatFailureError [agent.errors] → RepeatedFormatFailure
    │   │
    │   └── UserNoticeError            цикл идёт     → UserNoticeReady
    │
    ├── LLMFeedbackError            LLM видит через MessageService
    │   │
    │   ├── ToolFeedbackError       [agent.errors]   → role="tool" +
    │   │                                              ToolExecutionFailed
    │   │
    │   └── LLMToolCallFormatError  [agent.errors]   → role="user" +
    │                                                  ToolCallFormatFailed
    │
    └── Retryable                      миксин, ортогонально всему

Первичная ось классификации — **кому адресовано сообщение**
(user / LLM). Признак «терминальности» — флаг внутри user-ветки.
:class:`Retryable` — ортогональный маркер для авто-повтора.


════════════════════════════════════════════════════════════════════
  Сценарий → что бросать (quick reference)
════════════════════════════════════════════════════════════════════

┌──────────────────────────────────┬────────────────────────────────┐
│ Сценарий                         │ Что бросать                    │
├──────────────────────────────────┼────────────────────────────────┤
│ LLM упала/недоступна             │ LLMError / подкласс            │
│ Промпт не собрался               │ PromptError (agent.prompt)     │
│ Хранилище сообщений недоступно   │ MessageStoreError (agent.msg)  │
│ Tool упал (LLM должна увидеть)   │ ToolFeedbackError              │
│ LLM сломала формат tool call     │ LLMToolCallFormatError         │
│ Нотис пользователю               │ UserNoticeError (core.errors)  │
│ Исчерпан лимит итераций цикла    │ MaxIterationsExceededError     │
│ Модель залипла на сбое формата   │ RepeatedFormatFailureError     │
│ Добавить авто-повтор             │ + миксин Retryable             │
└──────────────────────────────────┴────────────────────────────────┘


════════════════════════════════════════════════════════════════════
  Примеры
════════════════════════════════════════════════════════════════════

**Остановить цикл**::

    from boba.domain.agent.errors import LLMContextLengthError
    raise LLMContextLengthError("context window exceeded", status_code=400)

    from boba.domain.agent.prompt import PromptProviderError
    try:
        data = read_source(...)
    except OSError as e:
        raise PromptProviderError(f"cannot read: {e}", provider="git") from e

    from boba.domain.agent.messages import MessageStoreWriteError
    try:
        storage.write(message)
    except OSError as e:
        raise MessageStoreWriteError(e, ctx=f"role={message.role}") from e

**Сказать LLM про ошибку tool** (``ToolExecutionMiddleware`` обогащает
«сырую» :class:`~boba.domain.core.tools.ToolExecutionError`
идентификатором tool call'а)::

    from boba.domain.agent.errors import ToolFeedbackError
    raise ToolFeedbackError(
        tool_call_id=tc.tool_call_id,
        tool_name=tc.tool_name,
        error_kind="ToolExecutionError",
        message="file not found: foo.txt",
    )

**Показать нотис пользователю**::

    from boba.domain.core.errors import UserNoticeError
    raise UserNoticeError(
        "Выбранная модель устарела, переключитесь на новую.",
        severity="warning",  # "info" | "warning" | "error"
    )

**Сделать ошибку повторяемой** — миксин ``Retryable``::

    from boba.domain.core.errors import Retryable
    from boba.domain.agent.prompt import PromptError

    class PromptLockFileError(PromptError, Retryable):
        '''Файл временно залочен — повторим.'''


════════════════════════════════════════════════════════════════════
  Batch-middleware (несколько подзадач в одном стриме)
════════════════════════════════════════════════════════════════════

Если middleware выполняет пачку подзадач и одна упавшая не должна
обрывать остальные (пример: ``ToolExecutionMiddleware`` на N тулах),
передай генераторы подзадач в ``AgentErrorRouter.run_batch``::

    yield from self._error_router.run_batch(
        ctx, (self._run_task(item) for item in items)
    )

Успешные события стримятся сразу. :class:`LLMFeedbackError` копится и
маршрутизируется после завершения всей серии. Терминальные ошибки
пропускаются наверх немедленно — batch прерывается (при сбое LLM /
persistence дальше работать смысла нет).

``try/except`` писать не надо.


════════════════════════════════════════════════════════════════════
  Как добавить новый тип ошибки
════════════════════════════════════════════════════════════════════

1. Унаследуй от подходящего маркера (в ``core.errors``):
   ``TerminalError`` / ``LLMFeedbackError`` / ``UserFeedbackError``.
   Если твой сервис живёт в ``core`` и не должен зависеть от
   ``agent`` — это правильная точка зависимости.
2. В ``__init__`` добавь поля контекста, которые роутер прокинет в
   событие (``status_code``, ``tool_call_id``, ``provider``, …).
3. Добавь ветку match-case в ``AgentErrorRouter.route`` (``meat.py``)
   — какое событие эмитить, какой сайд-эффект сделать.
4. Если нужно новое событие — добавь dataclass в ``events.py``,
   занеси в ``AgentEvent``-union и ``AgentEventName``-literal.
5. Обнови sink'и (``ConsoleSink`` / ``HistorySink``), если новый тип
   требует отображения/логгирования.

Пропущенная ветка в роутере → runtime ``TypeError`` с подсказкой,
какой тип не покрыт. Тихого проглатывания не будет.


════════════════════════════════════════════════════════════════════
  Что НЕ должно быть RoutableError
════════════════════════════════════════════════════════════════════

Программные баги (``KeyError``, ``AttributeError``, ``TypeError``,
``ValueError`` на boundary-нарушениях) — **пусть летят мимо** и крашат
процесс. Маскировать баги доменными ошибками запрещено — иначе баг
ляжет в UI или LLM как легитимный сигнал.

При переупаковке всегда сохраняй причину: ``raise NewError(...) from e``.
"""

from __future__ import annotations

from boba.domain.core.errors import (
    LLMFeedbackError,
    Retryable,
    TerminalError,
)


class LLMError(TerminalError):
    """Базовая доменная ошибка обращения к LLM.

    Адаптеры провайдеров (OpenAI, Ollama, …) ловят сырые исключения
    (``openai.*``, ``httpx.*``) и конвертируют их в потомков этого типа.
    Выше по стеку код работает только с доменными типами.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class RetryableLLMError(LLMError, Retryable):
    """Ошибка LLM, которую имеет смысл повторить: сеть, таймаут, rate-limit, 5xx."""


class PermanentLLMError(LLMError):
    """Ошибка LLM, которую повторять бессмысленно: auth, bad request, context length."""


class LLMConnectionError(RetryableLLMError):
    """Не удалось установить соединение с провайдером (DNS, refused, reset)."""


class LLMTimeoutError(RetryableLLMError):
    """Превышен таймаут запроса/чтения ответа."""


class LLMRateLimitError(RetryableLLMError):
    """Провайдер ответил 429 Too Many Requests."""


class LLMProviderInternalError(RetryableLLMError):
    """Провайдер ответил 5xx или оборвал стрим по внутренней причине."""


class LLMAuthError(PermanentLLMError):
    """Провайдер ответил 401/403 — неверный или отозванный ключ."""


class LLMInvalidRequestError(PermanentLLMError):
    """Провайдер ответил 400 — запрос сформирован некорректно."""


class LLMContextLengthError(PermanentLLMError):
    """Суммарная длина сообщений превысила окно модели."""


class LLMProtocolError(PermanentLLMError):
    """Провайдер вернул ответ вне ожидаемой схемы.

    Пример: ``finish_reason`` пришёл со значением, которого нет в
    :class:`~boba.domain.agent.events.FinishReason`. Retry не поможет —
    провайдер вернёт ту же структуру. Адаптер должен поднять эту ошибку
    при любом рассогласовании с контрактом (неизвестный enum, отсутствие
    обязательного поля, неверный тип).
    """


class MaxIterationsExceededError(TerminalError):
    """Агентский цикл исчерпал лимит итераций без финального ответа.

    Поднимается :class:`~boba.domain.agent.meat.IterationCounterMiddleware`,
    когда ``ctx.iteration`` превысил ``ctx.config.max_iterations``. Роутер
    конвертирует в :class:`~boba.domain.agent.events.MaxIterationsReached`,
    а :class:`~boba.domain.agent.meat.StopOnAnyFailure` ловит событие и
    останавливает цикл.
    """

    def __init__(self, message: str, *, limit: int, iteration: int) -> None:
        super().__init__(message)
        self.limit = limit
        self.iteration = iteration


class RepeatedFormatFailureError(TerminalError):
    """Модель N раз подряд вывела неверный формат tool call.

    Поднимается :class:`~boba.domain.agent.meat.RepeatedFormatFailureGuardMiddleware`
    после накопления ``limit`` подряд
    :class:`~boba.domain.agent.events.ToolCallFormatFailed`
    без успешного `ToolResultReady`. Роутер конвертирует в
    :class:`~boba.domain.agent.events.RepeatedFormatFailure`;
    :class:`~boba.domain.agent.meat.StopOnAnyFailure` ловит событие и
    останавливает цикл — от дальнейших «объяснений» формата модель всё
    равно не выходит из залипания.
    """

    def __init__(self, message: str, *, count: int, limit: int) -> None:
        super().__init__(message)
        self.count = count
        self.limit = limit


class ToolFeedbackError(LLMFeedbackError):
    """Ошибка выполнения конкретного tool'а.

    Роутер пишет ``LLMMessage`` с ``role="tool"``,
    ``tool_call_id=<tool_call_id>``, ``content=<message>`` и эмитит
    ``ToolExecutionFailed``.

    Поднимается ``ToolExecutionMiddleware``-ом (обогащает «сырую»
    :class:`~boba.domain.core.tools.ToolExecutionError` из
    :class:`~boba.domain.core.tools.ToolsService` идентификатором
    tool call'а).
    """

    def __init__(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        error_kind: str,
        message: str,
    ) -> None:
        super().__init__(message)
        self.tool_call_id = tool_call_id
        self.tool_name = tool_name
        self.error_kind = error_kind
        self.message = message


class LLMToolCallFormatError(LLMFeedbackError):
    """LLM нарушила формат content-as-JSON tool call'а.

    Роутер пишет ``LLMMessage`` с ``role="user"``, ``content=<message>``
    в :class:`MessageService` и эмитит :class:`ToolCallFormatFailed`.
    Цикл не прерывается — LLM на следующей итерации увидит критику своего
    предыдущего вывода и сможет переформулировать tool call.

    Поднимается парсером content-as-JSON
    (:class:`~boba.domain.agent.meat.StrictJsonToolCallParser`), когда
    LLM эмитит JSON-объект с неверной структурой (невалидный JSON,
    не-объект на корне, отсутствующие/посторонние поля, неверные типы).

    ``message`` уже включает цитату сырого content'а и описание, что
    именно не так — LLM получает самодостаточный фидбек.
    """

    def __init__(
        self,
        message: str,
        *,
        raw_content: str,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.raw_content = raw_content

class LLMRequestUserpromptNoneError(TerminalError):
    """Отсутствует user prompt"""

class LLMRequestModelNoneError(TerminalError):
    """Отсутствует model"""
