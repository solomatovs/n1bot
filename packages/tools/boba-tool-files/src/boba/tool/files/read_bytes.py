"""Tool: побайтовое чтение файла (окно [start_byte, end_byte))."""

from __future__ import annotations

import base64
from typing import Annotated, Any

from pydantic import Field

from boba.tool.files.config import FilesPluginConfig
from boba.tools import FromConfig, FromDI, Scope, tool
from boba.workspace.contract import (
    ProjectWorkspaceShell,
    WorkspaceError,
    WorkspaceNotFoundError,
)

__all__ = ["read_bytes"]


@tool
def read_bytes(  # noqa: PLR0913
    shell: Annotated[ProjectWorkspaceShell, FromDI(Scope.APP)],
    cfg: Annotated[FilesPluginConfig, FromConfig()],
    path: Annotated[str, Field(min_length=1, description="Путь к файлу.")],
    start_byte: Annotated[
        int,
        Field(ge=0, description="Смещение начала окна, 0-based."),
    ],
    end_byte: Annotated[
        int,
        Field(
            ge=0,
            description="Смещение конца окна (exclusive), end_byte >= start_byte.",
        ),
    ],
    encoding: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Сериализация content: 'base64', 'hex' или имя текстовой "
                "кодировки ('utf-8', 'latin-1', 'cp1251', ...). "
                "Для бинарных файлов используй 'base64'/'hex'."
            ),
        ),
    ],
) -> dict[str, Any]:
    """
    Чтение окна байт файла [start_byte, end_byte) с заданной сериализацией.

    Окно не шире `read_bytes_max_window` (default 200_000).
    При EOF возвращается фактический `end_byte`
    (может быть меньше запрошенного)
    """
    if end_byte < start_byte:
        raise RuntimeError(
            f"end_byte ({end_byte}) должна быть >= start_byte ({start_byte})",
        )
    window = end_byte - start_byte
    max_window = cfg.read_bytes_max_window
    if window > max_window:
        raise RuntimeError(
            f"Запрошенное окно {window} байт шире лимита {max_window}. "
            f"Читай порциями ≤ {max_window} байт: "
            f"start_byte={start_byte}, end_byte={start_byte + max_window}.",
        )

    try:
        with shell.read_binary(path) as f:
            f.seek(start_byte)
            data = f.read(window)
    except WorkspaceNotFoundError as e:
        raise RuntimeError(f"Файл не найден: {path}") from e
    except WorkspaceError as e:
        raise RuntimeError(f"Ошибка чтения: {e}") from e

    if encoding == "base64":
        content = base64.b64encode(data).decode("ascii")
    elif encoding == "hex":
        content = data.hex()
    else:
        try:
            content = data.decode(encoding)
        except LookupError as e:
            raise RuntimeError(
                f"Неизвестная кодировка '{encoding}'. Используй 'base64', "
                f"'hex' или валидную Python-кодировку (utf-8, latin-1, ...).",
            ) from e
        except UnicodeDecodeError as e:
            raise RuntimeError(
                f"Не удалось декодировать байты как '{encoding}' "
                f"(позиция {e.start}): {e.reason}. "
                f"Для бинарных данных используй encoding='base64' или 'hex'.",
            ) from e

    return {
        "path": path,
        "start_byte": start_byte,
        "end_byte": start_byte + len(data),
        "encoding": encoding,
        "content": content,
    }
