"""Agent-специфичные доменные ошибки + практический гайд по всей иерархии.

Этот модуль держит LLM-иерархию (:class:`LLMError` + подклассы) и
:class:`ToolFeedbackError`. Базовые маркеры (``RoutableError``,
``TerminalError``, ``UserFeedbackError``, ``LLMFeedbackError``,
``Retryable``, ``UserNoticeError``) живут в
:mod:`boba.domain.core.errors` — их используют и core-сервисы
(``core.promt``, ``core.history``), и agent-слой.


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
    │   │   │       ├── LLMContextLengthError
    │   │   │       └── LLMResponseFormatError
    │   │   │
    │   │   ├── PromptError         [core.promt]     → PromptFailed
    │   │   │   ├── RetryablePromptError (+ Retryable)
    │   │   │   └── PermanentPromptError
    │   │   │       └── PromptProviderError
    │   │   │
    │   │   └── HistoryError        [core.history]   → PersistenceFailed
    │   │       ├── HistoryWriteError
    │   │       └── HistoryReadError
    │   │
    │   └── UserNoticeError            цикл идёт     → UserNoticeReady
    │
    ├── LLMFeedbackError            LLM видит через MessageService
    │   │
    │   └── ToolFeedbackError       [agent.errors]   → role="tool" +
    │                                                  ToolExecutionFailed
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
│ Промпт не собрался               │ PromptError (core.promt)       │
│ Журнал недоступен                │ HistoryError (core.history)    │
│ Tool упал (LLM должна увидеть)   │ ToolFeedbackError              │
│ Нотис пользователю               │ UserNoticeError (core.errors)  │
│ Добавить авто-повтор             │ + миксин Retryable             │
└──────────────────────────────────┴────────────────────────────────┘


════════════════════════════════════════════════════════════════════
  Примеры
════════════════════════════════════════════════════════════════════

**Остановить цикл**::

    from boba.domain.agent.errors import LLMContextLengthError
    raise LLMContextLengthError("context window exceeded", status_code=400)

    from boba.domain.core.promt import PromptProviderError
    try:
        data = read_source(...)
    except OSError as e:
        raise PromptProviderError(f"cannot read: {e}", provider="git") from e

    from boba.domain.core.history import HistoryWriteError
    try:
        storage.write(entry)
    except OSError as e:
        raise HistoryWriteError(e, ctx=f"entry_id={entry.id}") from e

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
    from boba.domain.core.promt import PromptError

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


class LLMResponseFormatError(PermanentLLMError):
    """Ответ провайдера не удалось распарсить в ожидаемую структуру."""


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
