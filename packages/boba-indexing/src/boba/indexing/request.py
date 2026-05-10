"""
Request: общий Protocol для всех request-DTO.

Конкретные Request-DTO живут в transport-пакетах:
- `HttpRequest` в `boba-ext-http-transport` (url + method + headers + auth).
- `FsRequest` в `boba-ext-fs-transport` (path).

Pipeline и `RequestSource[ReqT]` параметризуются конкретным типом —
generic-параметр `ReqT` ограничен этим protocol'ом, что даёт type-safety
(HttpTransport не примет FsRequest).


RequestSource - источник Request-планов для Transport'а.

- `RequestSource[HttpRequest]` для REST-API
- `RequestSource[FsRequest]` для файловой системы.

Pipeline и Transport параметризуются тем же типом — type-checker
не даст совместить несовместимые слои.

source_id формирует RequestSource
он один знает связь между URL/path'ом и логическим документом
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable
from typing import Protocol, TypeVar, runtime_checkable

from boba.indexing.context import PipelineContext
from boba.indexing.errors import SyncUnsupportedError
from boba.indexing.metadata import Metadata
from boba.indexing.sections import SourceId
from boba.patterns import StreamSource

__all__ = ["Request", "RequestSource"]


@runtime_checkable
class Request(Protocol):
    """
    Контракт Request-DTO:
        `source_id` - сообщает название источника документа.
            Это название прокидывается далее во все чанки сквозным образом
        `metadata` - сообщает исходные метаданные документа.
            Эти метаданные в процессе выполнения pipeline обогощаются и записываются в чанки
            Здесь можно сообщить о документе базовую исходную информацию, которую хочется
            Донести до каждого чанка

    Контракты живут в отдельных transport-пакетах, например:
    - `HttpRequest`  -> Request  — url, method, headers, auth.
    - `FsRequest`    -> Request  — path.
    """  # noqa: E501

    @property
    def source_id(self) -> SourceId: ...

    @property
    def metadata(self) -> Metadata: ...


ReqT = TypeVar("ReqT", bound=Request)


class RequestSource(StreamSource[PipelineContext, ReqT]):
    """
    Источник Request'ов для Transport'а

    Источник нужен что бы генерировать Request'ы на выполнение в Transport

    Пример разных source'еров может быть процесс загрузки confluence-страниц, когда необходимо загрузить из confluence страницы в зависимости от стратегии:
    - `page_id source` - выдает request ровно на 1-у страницу
    - `space_key source` - последовательно выдает request'ы на зугрузку страниц внутри указанного space

    **Схема**:
    ```python
    source  ──────────────────────source.stream(ctx)──→  Iterable[ReqT]
    (config, fs-walk, API …)                          →    source_id : SourceId   (canonical id — формирует source)
                                                      →    metadata  : Metadata   (hint'ы для transport / reader / chunker)
                                                      →    <fields>               (url / path / …)
    ```

    `source_id` — каноничен и стабилен: именно RequestSource знает, как
    сопоставить URL/path с логическим документом. Дальше по pipeline
    этот id пробрасывается без изменений (Request → RawDocument → Section → Chunk).

    **Пример** (usage):
    ```python
    source: RequestSource[FsRequest] = FsWalkRequestSource(
        paths=[Path("docs/")],
        include=["*.md"],
    )

    list(source.stream(ctx)) == [
        FsRequest(
            path=Path("docs/intro.md"),
            source_id=SourceId("fs:/abs/docs/intro.md")),
        FsRequest(
            path=Path("docs/api.md"),
            source_id=SourceId("fs:/abs/docs/api.md")),
    ]
    ```
    """  # noqa: E501

    @abstractmethod
    def list_source_ids(self, ctx: PipelineContext) -> Iterable[str]:
        """Перечисляет canonical source_id'ы; `SyncUnsupportedError` если не реализовано."""
        del ctx
        raise SyncUnsupportedError(self.name())
