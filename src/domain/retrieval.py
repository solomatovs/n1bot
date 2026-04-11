from __future__ import annotations

from typing import Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Доменный контракт документа
# ---------------------------------------------------------------------------

@runtime_checkable
class DocumentLike(Protocol):
    """Минимальный контракт документа для domain-логики.

    langchain_core.documents.Document реализует этот Protocol автоматически.
    """

    @property
    def page_content(self) -> str: ...

    @property
    def metadata(self) -> dict: ...
