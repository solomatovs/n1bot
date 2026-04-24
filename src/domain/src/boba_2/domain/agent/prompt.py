"""Сборка LLM-промптов из провайдеров (fold-factory по :class:`PromptKind`).

Провайдеры поставляют блоки определённого типа (``SYSTEM`` / ``USER``);
:class:`PromptFactory` агрегирует их по приоритету в
:class:`PromptResult`. Middleware ``SystemPromptMiddleware`` и
``UserPromptMiddleware`` (:mod:`boba_2.domain.agent.meat.prompt`)
вызывают фабрику и пишут финальные тексты в :class:`MessageService`.

Параметр ``TCtx`` в :class:`PromptState` — тип контекста, прокидываемый
провайдерам. В boba_2 это :class:`~boba_2.domain.agent.models.\
AgentContext`, но сам модуль работает с любым типом через generics
— это делает :class:`PromptProvider` переиспользуемым вне агентского
слоя (например, для chainlit-prompt сборки).

════════════════════════════════════════════════════════════════════
  Иерархия ошибок
════════════════════════════════════════════════════════════════════

::

    PromptError(UserFeedbackError[...], TerminalError) → PromptFailed
    │   provider: str | None
    │
    ├── RetryablePromptError(+ Retryable)            транзиентная I/O
    └── PermanentPromptError
        └── PromptProviderError                      провайдер упал

``PromptFactory.build()`` оборачивает ``OSError`` → ``PromptProviderError``
автоматически. ``PromptError`` из провайдеров пропускается как есть
(например, провайдер сам валидирует и бросает ``PermanentPromptError``).
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Generic, Self, TypeVar

from boba.domain.core.errors import Retryable, TerminalError, UserFeedbackError
from boba.domain.core.patterns import FoldFactory, Id, PrioritySource
from boba_2.domain.agent.events import AgentEvent, PromptFailed
from boba_2.domain.llm.models import RequestId

TCtx = TypeVar("TCtx")


class PromptError(UserFeedbackError[RequestId, AgentEvent], TerminalError):
    """Базовая ошибка сборки промпта.

    Адаптеры-провайдеры (файлы, workspace, git) оборачивают свои
    I/O-исключения в потомков этого класса. Ошибки логики/валидации
    (неверный тип, битая регистрация) — это баги и должны падать
    напрямую, без конвертации в :class:`PromptError`.
    """

    def __init__(self, message: str, *, provider: str | None = None) -> None:
        super().__init__(message)
        self.provider = provider

    def to_event(self, request_id: RequestId) -> AgentEvent:
        return PromptFailed(
            request_id=request_id,
            error_kind=type(self).__name__,
            message=str(self),
            retryable=isinstance(self, Retryable),
            provider=self.provider,
        )


class RetryablePromptError(PromptError, Retryable):
    """Временная проблема провайдера (транзиентный I/O, локфайл)."""


class PermanentPromptError(PromptError):
    """Провайдер не может вернуть блок: нет файла/прав, битая конфигурация."""


class PromptProviderError(PermanentPromptError):
    """Провайдер упал на чтении своего источника.

    Автоматически поднимается :meth:`PromptFactory.build` при
    перехвате ``OSError`` от любого провайдера.
    """


class PromptKind(Enum):
    """Тип собираемого промпта.

    На S7-этапе — только ``SYSTEM`` и ``USER``. ``TOOLS_DEFINITION``
    из старого кода не портирован: в boba_2 каталог tools живёт в
    :class:`~boba_2.domain.agent.meat.tools.ToolsDefinitionMiddleware`
    как отдельная стадия, а не собирается через ту же prompt-фабрику.
    """

    SYSTEM = "system"
    USER = "user"


@dataclass(frozen=True)
class PromptBlock:
    """Один собранный блок промпта."""

    name: str
    content: str


class PromptId(Id[str]):
    """Идентификатор провайдера."""

    def to_wire(self) -> str:
        return self._name

    @classmethod
    def from_wire(cls, value: str) -> Self:
        return cls(value)


class PromptState(Generic[TCtx]):
    """Накапливаемое состояние сборки: blocks per-kind + контекст.

    Контекст передаётся провайдерам, чтобы они могли брать данные из
    текущего запроса (query, workspace, и т.д.).
    """

    def __init__(self, ctx: TCtx) -> None:
        self.ctx = ctx
        self.blocks: dict[PromptKind, list[PromptBlock]] = {}

    def add(self, kind: PromptKind, block: PromptBlock) -> None:
        self.blocks.setdefault(kind, []).append(block)


class PromptResult:
    """Финальная раскладка: блоки сгруппированы по :class:`PromptKind`."""

    def __init__(
        self,
        blocks_by_kind: Mapping[PromptKind, Iterable[PromptBlock]],
    ) -> None:
        self._by_kind: dict[PromptKind, list[PromptBlock]] = {
            k: list(v) for k, v in blocks_by_kind.items()
        }

    def blocks(self, kind: PromptKind) -> list[PromptBlock]:
        return list(self._by_kind.get(kind, []))

    def to_string(self, kind: PromptKind) -> str:
        """Конкатенация всех непустых блоков через двойной перенос строки."""
        return "\n\n".join(b.content for b in self._by_kind.get(kind, []) if b.content)


class PromptProvider(PrioritySource[PromptId, PromptState]):
    """Провайдер блоков промпта одного типа (:meth:`kind`).

    Реализации должны указывать:

    - :meth:`id` — уникальный идентификатор (для замены/удаления);
    - :meth:`priority` — меньше число → раньше в раскладке;
    - :meth:`kind` — к какому разделу добавляются блоки;
    - :meth:`blocks` — один или больше :class:`PromptBlock`.
    """

    @abstractmethod
    def kind(self) -> PromptKind: ...

    @abstractmethod
    def blocks(self, state: PromptState) -> Iterable[PromptBlock]: ...

    def apply(self, state: PromptState) -> PromptState:
        kind = self.kind()
        for block in self.blocks(state):
            state.add(kind, block)
        return state


class PromptFactory(FoldFactory[PromptId, PromptState[TCtx], PromptResult]):
    """Собирает :class:`PromptResult` из зарегистрированных провайдеров.

    Per-call экземпляр: :class:`PromptState` строится на лету под
    переданный ``ctx``. Контракт ошибок в :meth:`build`:

    - :class:`PromptError` из провайдера пробрасывается как есть;
    - ``OSError`` → :class:`PromptProviderError` (узкий wrap для
      адаптеров, которые не обернули свой I/O сами);
    - любые другие исключения (логика, программный баг) — пропускаются
      наружу и крашат процесс.
    """

    def __init__(self, ctx: TCtx, providers: Iterable[PromptProvider]) -> None:
        super().__init__()
        self._ctx = ctx
        for p in providers:
            self.register(p)

    def initial(self) -> PromptState:
        return PromptState(self._ctx)

    def finalize(self, state: PromptState) -> PromptResult:
        return PromptResult(state.blocks)

    def build(self) -> PromptResult:
        state = self.initial()
        for p in sorted(self.providers(), key=lambda p: p.priority()):
            try:
                state = p.apply(state)
            except PromptError:
                raise
            except OSError as e:
                raise PromptProviderError(
                    f"{type(e).__name__}: {e}",
                    provider=p.id().to_wire(),
                ) from e
        return self.finalize(state)
