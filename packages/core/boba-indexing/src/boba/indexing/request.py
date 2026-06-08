"""
Request: общий Protocol для всех request-DTO.

Конкретные Request-DTO живут в transport-пакетах:
- HttpRequest в boba-ext-http-transport (url + method + headers + auth).
- FsRequest в boba-ext-fs-transport (path).

Pipeline и RequestSource[ReqT] параметризуются конкретным типом —
generic-параметр ReqT ограничен этим protocol'ом, что даёт type-safety
(HttpTransport не примет FsRequest).


RequestSource - источник Request-планов для Transport'а.

- RequestSource[HttpRequest] для REST-API
- RequestSource[FsRequest] для файловой системы.

Pipeline и Transport параметризуются тем же типом — type-checker
не даст совместить несовместимые слои.

source_id (идентичность документа) формирует НЕ RequestSource, а Transport —
он резолвит реальный адрес запрошенного объекта (URL/path) и проставляет id
(см. Transport.source_id). RequestSource несёт только «что забрать» + metadata.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import Generic, Protocol, TypeVar, runtime_checkable

from boba.indexing.metadata import Metadata

__all__ = ["Request", "RequestSource"]


@runtime_checkable
class Request(Protocol):
    """
    Контракт Request-DTO — чистый план «что забрать»:
        metadata - сообщает исходные метаданные документа.
            Эти метаданные в процессе выполнения pipeline обогощаются и записываются в чанки
            Здесь можно сообщить о документе базовую исходную информацию, которую хочется
            Донести до каждого чанка

    source_id (идентичность документа) НЕ часть Request — её вычисляет Transport
    из реального адреса запрошенного объекта (URL/path), см. Transport.source_id.
    Так identity всегда совпадает с тем, что физически забирается, и не дрейфует.

    Контракты живут в отдельных transport-пакетах, например:
    - HttpRequest  -> Request  — url, method, headers, auth.
    - FsRequest    -> Request  — path.
    """  # noqa: E501

    @property
    def metadata(self) -> Metadata: ...


ReqT = TypeVar("ReqT", bound=Request)


class RequestSource(ABC, Generic[ReqT]):
    """
    Источник Request'ов для Transport'а

    Источник нужен что бы генерировать Request'ы на выполнение в Transport

    Пример разных source'еров может быть процесс загрузки confluence-страниц, когда необходимо загрузить из confluence страницы в зависимости от стратегии:
    - page_id source - выдает request ровно на 1-у страницу
    - space_key source - последовательно выдает request'ы на зугрузку страниц внутри указанного space

    **Схема**:
    python
    source  ──────────────────────source.requests()──->  Iterable[ReqT]
    (config, fs-walk, API …)                            ->    metadata  : Metadata   (hint'ы для transport / reader / chunker)
                                                        ->    <fields>               (url / path / …)


    source_id (идентичность документа) RequestSource НЕ формирует — её выводит
    Transport из реального адреса запрошенного объекта (см. Transport.source_id).
    RequestSource несёт только «что забрать» (<fields>) + логические/исходные
    hint'ы в metadata (canonical URL, page_id и т.п.).

    **Пример** (usage):
    python
    source: RequestSource[FsRequest] = FsWalkRequestSource(
        paths=[Path("docs/")],
        include=["*.md"],
    )

    list(source.requests()) == [
        FsRequest(path="docs/intro.md", metadata=…),
        FsRequest(path="docs/api.md",   metadata=…),
    ]
    # source_id (fs:/abs/…) проставит FsTransport.source_id

    """  # noqa: E501

    @abstractmethod
    def requests(self) -> Iterable[ReqT]:
        """Сгенерировать поток ReqT-планов для Transport'а."""
        ...
