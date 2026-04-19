"""Иерархия доменных ошибок агента — маршрутизация через тип.

Ошибки здесь **маркерные**: тип ошибки декларирует её маршрут, а не место
возникновения. Любой middleware может ``raise`` подкласс :class:`AgentError`
и не думать о последствиях — :class:`AgentErrorRouter` (см. ``meat.py``)
распознаёт тип через match-case и делает нужный side-effect + эмитит
соответствующее событие.

====================================================================
  Две feedback-ветки: кому адресовано сообщение
====================================================================

::

    AgentError
    ├── UserFeedbackError      «пользователь видит, LLM не видит»
    │   ├── TerminalError        + цикл стоп
    │   │   ├── LLMError
    │   │   ├── PromptError
    │   │   └── HistoryError
    │   └── UserNoticeError      цикл идёт (нотис)
    └── LLMFeedbackError       «LLM видит через MessageService»
        └── ToolFeedbackError

Первичная ось — **адресат** (user / LLM). Признак «терминальности» —
флаг внутри user-ветки (:class:`TerminalError`). Маркер :class:`Retryable`
ортогонален всему — миксин для auto-retry.

====================================================================
  Сценарий → что бросать
====================================================================

**Остановить цикл, показать ошибку пользователю** (терминальные):

- LLM недоступна/упала → :class:`LLMError` (или подкласс:
  :class:`LLMRateLimitError`, :class:`LLMAuthError`,
  :class:`LLMContextLengthError`, …). Роутер → :class:`GenerationFailed`.
- Промпт не собрался → ``PromptError`` (см. ``core.promt``).
  Роутер → :class:`PromptFailed`.
- Журнал недоступен → ``HistoryError`` (см. ``core.history``).
  Роутер → :class:`PersistenceFailed`.

**Сказать LLM, что подзадача упала** (не терминально, цикл идёт):

- Tool'а упал → :class:`ToolFeedbackError` (несёт ``tool_call_id``).
  Роутер пишет ``LLMMessage(role="tool", content=...)`` в
  :class:`MessageService` + эмитит :class:`ToolExecutionFailed`.
  LLM на следующей итерации увидит ошибку и сможет починиться.

**Показать пользователю нотис** (не терминально, LLM не видит):

- :class:`UserNoticeError` (``severity``: ``"info"`` / ``"warning"`` /
  ``"error"``). Роутер → :class:`UserNoticeReady`.

**Авто-повтор** (ортогональный маркер):

- Подмешай :class:`Retryable` в любой из типов выше — retry-слой
  поймает по маркеру до ``max_retries`` попыток. После исчерпания
  ошибка летит дальше и обрабатывается по своему основному типу.

====================================================================
  Примеры
====================================================================

Остановить цикл::

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

Сказать LLM про ошибку tool (поднимается в :class:`ToolExecutionMiddleware`
при перехвате «сырой» :class:`~boba.domain.core.tools.ToolExecutionError`)::

    from boba.domain.agent.errors import ToolFeedbackError
    raise ToolFeedbackError(
        tool_call_id=tc.tool_call_id,
        tool_name=tc.tool_name,
        error_kind="ToolExecutionError",
        message="file not found: foo.txt",
    )

Показать нотис пользователю::

    from boba.domain.agent.errors import UserNoticeError
    raise UserNoticeError(
        "Выбранная модель устарела, переключитесь на новую.",
        severity="warning",
    )

Сделать ошибку повторяемой — миксин :class:`Retryable`::

    from boba.domain.agent.errors import Retryable
    from boba.domain.core.promt import PromptError

    class PromptLockFileError(PromptError, Retryable):
        '''Файл временно залочен — повторим.'''

====================================================================
  Batch-семантика (несколько подзадач в одном middleware)
====================================================================

Если middleware выполняет пачку подзадач (как :class:`ToolExecutionMiddleware`
несколько tool calls), и хочется, чтобы одна упавшая подзадача не оборвала
остальные — передай генераторы подзадач в :meth:`AgentErrorRouter.run_batch`::

    yield from self._error_router.run_batch(
        ctx, (self._run_task(item) for item in items)
    )

Успешные события стримятся сразу, :class:`LLMFeedbackError` копится и
маршрутизируется после завершения всей серии. Терминальные ошибки
пропускаются наверх немедленно — batch прерывается.

Middleware-автор свой ``try/except`` не пишет.

====================================================================
  Как добавить новый тип ошибки
====================================================================

1. Унаследуйся от :class:`TerminalError` / :class:`LLMFeedbackError` /
   :class:`UserFeedbackError` — в зависимости от нужного маршрута.
2. Добавь в ``__init__`` поля контекста, которые роутер должен передать
   в событие (``status_code``, ``tool_call_id``, …).
3. Добавь ветку в ``AgentErrorRouter.route`` (``meat.py``) — какое
   событие эмитить и какой сайд-эффект сделать.
4. Если нужно новое событие — добавь dataclass в ``events.py``, занеси
   в ``AgentEvent``-union и ``AgentEventName``-literal.
5. Обнови sink'и (``ConsoleSink``, ``HistorySink``), если новый тип
   требует отображения/логгирования.

Если ветка в роутере пропущена — ``AgentErrorRouter.route`` в runtime
кинет :class:`TypeError` с подсказкой, какой тип не покрыт.

====================================================================
  Что НЕ должно быть AgentError
====================================================================

Программные баги (``KeyError``, ``AttributeError``, ``TypeError``,
``ValueError`` на boundary-нарушениях) — пусть летят мимо и крашат
процесс. Маскировать баги доменными ошибками **запрещено** — иначе
поведение станет неотличимо от бизнес-ошибки и ляжет в LLM/UI как
легитимный сигнал.

При переупаковке всегда сохраняй причину: ``raise NewError(...) from e``.
"""

from __future__ import annotations

from typing import Literal


class AgentError(Exception):
    """Базовый класс всех **маршрутизируемых** ошибок агента.

    Подкласс этого типа == «роутер знает, что с этим делать». Всё, что
    не наследует ``AgentError``, считается багом и не обрабатывается.

    Делится по оси «кому адресовано сообщение»:

    - :class:`UserFeedbackError` — видит пользователь через UI-sink,
      LLM не видит;
    - :class:`LLMFeedbackError` — видит LLM через :class:`MessageService`.

    Признак «терминальности» (цикл стоп / цикл идёт) — вторичен по
    отношению к адресату; маркируется классом :class:`TerminalError`
    внутри user-ветки.
    """


class UserFeedbackError(AgentError):
    """Сообщение, адресованное **пользователю**.

    Пользователь увидит через UI-sink. LLM **не** видит — в
    :class:`MessageService` не пишется.

    Делится на:

    - :class:`TerminalError` — ошибка, после которой цикл агента
      останавливается (:class:`LLMError`, :class:`PromptError`,
      :class:`HistoryError`);
    - не-терминальные нотисы типа :class:`UserNoticeError` — цикл
      продолжается, сообщение показывается как информационное.
    """


class TerminalError(UserFeedbackError):
    """Ошибка прерывает текущий запрос и превращается в ``*Failed``-событие.

    Подкласс :class:`UserFeedbackError`: пользователь видит сообщение.
    Плюс :class:`StopOnAnyFailure` останавливает цикл.

    Подклассы определяют *какое именно* ``*Failed``-событие будет
    эмитнуто (роутер делает match-case по конкретному типу —
    :class:`LLMError` → :class:`GenerationFailed`, :class:`PromptError` →
    :class:`PromptFailed`, :class:`HistoryError` →
    :class:`PersistenceFailed`).
    """


UserNoticeSeverity = Literal["info", "warning", "error"]


class UserNoticeError(UserFeedbackError):
    """Нотис пользователю с уровнем важности — не-терминальный.

    Роутер эмитит :class:`UserNoticeReady(message=..., severity=...)`.
    Use-cases: warnings, deprecation-нотисы, soft-rejects валидации.
    """

    def __init__(
        self, message: str, *, severity: UserNoticeSeverity = "info"
    ) -> None:
        super().__init__(message)
        self.message = message
        self.severity: UserNoticeSeverity = severity


class LLMFeedbackError(AgentError):
    """Ошибка, которую надо отдать **обратно LLM** как tool-/assistant-сообщение.

    Роутер пишет содержимое в :class:`MessageService` как ``LLMMessage``
    соответствующей роли и эмитит наблюдательное событие для sink'ов. Цикл
    агента продолжается — LLM на следующей итерации увидит ошибку и
    сможет её скорректировать.

    Конкретный тип подкласса определяет формат сообщения (роль, куда
    прикрепить — ``tool_call_id`` и т.п.). Роутер делает match-case.
    """


class ToolFeedbackError(LLMFeedbackError):
    """Ошибка выполнения конкретного tool'а.

    Роутер пишет :class:`LLMMessage` с ``role="tool"``,
    ``tool_call_id=<tool_call_id>``, ``content=<message>`` и эмитит
    :class:`ToolExecutionFailed`.

    Поднимается :class:`ToolExecutionMiddleware`-ом (обогащает
    «сырую» :class:`ToolExecutionError` из :class:`ToolsService`
    идентификатором tool call'а).
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


class Retryable(AgentError):  # noqa: N818
    """Маркер-миксин: ошибку имеет смысл повторить.

    Ортогонален :class:`TerminalError` / :class:`LLMFeedbackError` /
    :class:`UserFeedbackError`: retry-слой ловит по этому маркеру,
    не зная конкретной причины. После исчерпания попыток ошибка летит
    дальше и обрабатывается по своему основному типу.

    Примеры композиции: ``class RetryableLLMError(LLMError, Retryable)``.
    """


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
