"""Сборка system-prompt из провайдеров (fold-factory).

Провайдеры поставляют PromptBlock-и; PromptFactory
агрегирует их по приоритету в PromptResult и склеивает в
строку. Используется
SystemPromptReducer каждую
итерацию — содержимое пересобирается per-call, провайдеры могут
реагировать на ctx.agent (workspace, iteration и т.д.).

USER-сообщение через эту фабрику **не** идёт. Пользовательский
ввод приходит уже отформатированным в query
и кладётся в MessageService агентом первой операцией —
обогащение (IDE selection, шаблоны и пр.) — ответственность caller'а
(frontend/CLI), а не агентского слоя.

Параметр TCtx в PromptState — тип контекста, прокидываемый
провайдерам. В boba это \
AgentContext, но сам модуль работает с любым типом через generics
— это делает PromptProvider переиспользуемым вне агентского
слоя.

════════════════════════════════════════════════════════════════════
  Иерархия ошибок
════════════════════════════════════════════════════════════════════

::

    PromptError(TerminalError[RequestId, AgentEvent]) → PromptFailed
    │   provider: str | None

PromptFactory.build() оборачивает OSError → PromptProviderError
автоматически. PromptError из провайдеров пропускается как есть
(например, провайдер сам валидирует и бросает PermanentPromptError).
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Generic, Self, TypeVar

from boba.domain.agent.events import AgentEvent, PromptFailed
from boba.domain.core.errors import TerminalError
from boba.domain.core.patterns import FoldFactory, Id, PrioritySource
from boba.domain.llm.models import RequestId

TCtx = TypeVar("TCtx")


class PromptError(TerminalError[RequestId, AgentEvent]):
    """Базовая ошибка сборки промпта.

    Адаптеры-провайдеры (файлы, workspace, git) оборачивают свои
    I/O-исключения в потомков этого класса. Ошибки логики/валидации
    (неверный тип, битая регистрация) — это баги и должны падать
    напрямую, без конвертации в PromptError.
    """

    def __init__(self, message: str, *, provider: str | None = None) -> None:
        super().__init__(message)
        self.provider = provider

    def to_user_feedback(self, request_id: RequestId) -> PromptFailed:
        return PromptFailed(
            request_id=request_id,
            error_kind=type(self).__name__,
            message=str(self),
            provider=self.provider,
        )


class PermanentPromptError(PromptError):
    """Провайдер не может вернуть блок: нет файла/прав, битая конфигурация."""


class PromptProviderError(PermanentPromptError):
    """Провайдер упал на чтении своего источника.

    Автоматически поднимается build при
    перехвате OSError от любого провайдера.
    """


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
    """Накапливаемое состояние сборки: список blocks + контекст.

    Контекст передаётся провайдерам, чтобы они могли брать данные из
    текущего запроса (workspace, iteration и т.д.).
    """

    def __init__(self, ctx: TCtx) -> None:
        self.ctx = ctx
        self.blocks: list[PromptBlock] = []

    def add(self, block: PromptBlock) -> None:
        self.blocks.append(block)


class PromptResult:
    """Финальная раскладка собранных блоков."""

    def __init__(self, blocks: Iterable[PromptBlock]) -> None:
        self._blocks: list[PromptBlock] = list(blocks)

    def blocks(self) -> list[PromptBlock]:
        return list(self._blocks)

    def to_string(self) -> str:
        """Конкатенация всех непустых блоков через двойной перенос строки."""
        return "\n\n".join(b.content for b in self._blocks if b.content)


class PromptProvider(PrioritySource[PromptId, PromptState]):
    """Провайдер блоков system-prompt.

    Реализации должны указывать:

    - id — уникальный идентификатор (для замены/удаления);
    - priority — меньше число → раньше в раскладке;
    - blocks — один или больше PromptBlock.
    """

    @abstractmethod
    def blocks(self, state: PromptState) -> Iterable[PromptBlock]: ...

    def apply(self, state: PromptState) -> PromptState:
        for block in self.blocks(state):
            state.add(block)
        return state


class PromptFactory(FoldFactory[PromptId, PromptState[TCtx], PromptResult]):
    """Собирает PromptResult из зарегистрированных провайдеров.

    Per-call экземпляр: PromptState строится на лету под
    переданный ctx. Контракт ошибок в build:

    - PromptError из провайдера пробрасывается как есть;
    - OSError → PromptProviderError (узкий wrap для
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
