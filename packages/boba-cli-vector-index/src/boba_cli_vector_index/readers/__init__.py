"""Реестр reader'ов: file extension → :class:`Reader`.

Регистрируются здесь — extras-reader'ы (``html``, ``confluence``)
импортируются лениво в момент первого вызова, чтобы отсутствие
optional dependency не ломало базовый импорт пакета.
"""

from __future__ import annotations

from pathlib import Path

from boba_cli_vector_index.readers._base import Document, Reader
from boba_cli_vector_index.readers.md import MarkdownReader
from boba_cli_vector_index.readers.txt import TextReader

__all__ = ["Document", "Reader", "UnsupportedFormatError", "reader_for"]


# Базовые reader'ы (всегда доступны, без extras).
_BUILTIN_READERS: tuple[Reader, ...] = (
    MarkdownReader(),
    TextReader(),
)


class UnsupportedFormatError(Exception):
    """Расширение файла не покрывается ни одним из зарегистрированных
    reader'ов (учитывая установленные extras).
    """

    def __init__(self, path: str) -> None:
        suffix = Path(path).suffix or "(no suffix)"
        super().__init__(
            f"no reader registered for {suffix!r} ({path}). "
            f"Install extras: e.g. "
            f"`pip install boba-cli-vector-index[html]` or [confluence]."
        )
        self.path = path


def reader_for(path: str) -> Reader:
    """Найти reader для файла по его расширению. Бросает
    :class:`UnsupportedFormatError`, если ни один встроенный или
    extras-reader не подходит.
    """
    suffix = Path(path).suffix.lower()
    for r in _BUILTIN_READERS:
        if suffix in r.extensions:
            return r
    extras_reader = _try_extras_reader(suffix)
    if extras_reader is not None:
        return extras_reader
    raise UnsupportedFormatError(path)


def _try_extras_reader(suffix: str) -> Reader | None:
    """Лениво импортирует optional reader'ы. Если соответствующая
    deps-группа не установлена — возвращает ``None`` (caller получит
    ``UnsupportedFormatError`` с подсказкой про extras).
    """
    if suffix in (".html", ".htm"):
        try:
            from boba_cli_vector_index.readers.html import HtmlReader  # noqa: PLC0415
        except ImportError:
            return None
        return HtmlReader()
    # confluence пока без файлового расширения — будет отдельный
    # ``boba-cli-vector-index index-confluence ...`` подпайплайн позже.
    return None
