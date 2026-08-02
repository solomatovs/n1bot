"""HTTP-транспорт, который закрывается при остановке хода.

Скачивание страницы держит рабочий поток внутри httpx, где проверить флаг
отмены нечем. Закрытие клиента обрывает соединение и снимает поток с чтения,
поэтому close регистрируется прерывателем на всё время жизни транспорта.
"""

from __future__ import annotations

from contextlib import AbstractContextManager

from boba.chainlit2.agent.cancellation import current_cancellation
from boba.transport.http import HttpProfile, HttpTransport

__all__ = ["CancellableHttpTransport"]


class CancellableHttpTransport(HttpTransport):
    """HttpTransport, обрываемый остановкой хода.

    Прерыватель снимается в close, поэтому повторный close после остановки
    безопасен: базовый HttpTransport закрывает клиент идемпотентно.
    """

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
