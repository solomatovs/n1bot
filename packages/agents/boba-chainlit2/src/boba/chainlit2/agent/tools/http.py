"""HTTP-транспорт, который закрывается при остановке хода."""

from __future__ import annotations

from contextlib import AbstractContextManager

from boba.chainlit2.agent.cancellation import current_cancellation
from boba.transport.http import HttpProfile, HttpTransport

__all__ = ["CancellableHttpTransport"]


class CancellableHttpTransport(HttpTransport):
    """HttpTransport, обрываемый остановкой хода."""

    def __init__(self, profile: HttpProfile) -> None:
        super().__init__(profile)
        cancellation = current_cancellation()
        cancellation.raise_if_cancelled()
        self._abort: AbstractContextManager[None] = cancellation.abort_with(self.close)
        self._abort.__enter__()

    def close(self) -> None:
        abort = self.__dict__.pop("_abort", None)
        if abort is not None:
            abort.__exit__(None, None, None)
        super().close()
