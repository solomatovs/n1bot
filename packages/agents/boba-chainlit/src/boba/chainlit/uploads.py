"""Сохранение user-uploaded файлов из Chainlit в project-WorkspaceShell.

Файлы складываются в фиксированную поддиректорию upload/ внутри workspace
сессии — туда же, куда смотрят tool'ы cat/grep/ls из boba-tool-files.
Конфликт имён разрешается перезаписью (последний загруженный файл — актуальный).
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import unicodedata
from collections.abc import Iterable
from typing import Any

from boba.workspace.contract import WorkspaceShell

__all__ = ["UPLOAD_DIR", "save_user_uploads"]

logger = logging.getLogger(__name__)

UPLOAD_DIR = "upload"

_FS_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_NAME_MAX = 120


def _sanitize_filename(name: str) -> str:
    """Привести к Unicode-NFC, заменить запрещённые символы на _, обрезать длину.

    NFC обязателен: клиенты на macOS/iOS присылают имена в разложенной форме
    (NFD, например й = и + комбинирующая бреве), а LLM и остальная система
    оперируют NFC. Без канонизации lookup файла по имени (read_binary/cat/...)
    не совпадает с записью на диске.
    """
    normalized = unicodedata.normalize("NFC", name)
    cleaned = _FS_FORBIDDEN.sub("_", normalized).strip(" .")
    if len(cleaned) > _NAME_MAX:
        cleaned = cleaned[:_NAME_MAX].rstrip(" .")
    return cleaned or "_"


async def save_user_uploads(
    elements: Iterable[Any],
    shell: WorkspaceShell,
) -> list[str]:
    """Сохранить attachment'ы cl.Message.elements в upload/ workspace'а.

    Возвращает список workspace-relative путей сохранённых файлов в порядке
    поступления. Конфликт имён — перезапись. Запись стримом через
    atomic_write_binary: содержимое не загружается в память.
    """
    saved: list[str] = []
    for el in elements:
        name = getattr(el, "name", None)
        if not name:
            continue
        rel = f"{UPLOAD_DIR}/{_sanitize_filename(name)}"
        path = getattr(el, "path", None)
        content = getattr(el, "content", None)
        try:
            if path:
                await asyncio.to_thread(_save_from_disk, shell, rel, path)
            elif content is not None:
                await asyncio.to_thread(
                    _save_from_memory, shell, rel, content,
                )
            else:
                logger.debug(
                    "upload skip: element %r has no .path nor .content", name,
                )
                continue
            saved.append(rel)
        except Exception:
            logger.exception("upload failed: %r", name)
    return saved


def _save_from_disk(
    shell: WorkspaceShell, rel: str, src_path: str,
) -> None:
    """Stream tmp-файл Chainlit'а в workspace — без чтения в память."""
    with open(src_path, "rb") as fh:
        shell.atomic_write_binary(rel, fh)


def _save_from_memory(
    shell: WorkspaceShell, rel: str, content: Any,
) -> None:
    """Принять bytes/bytearray/memoryview/str -> atomic_write_binary."""
    if isinstance(content, (bytes, bytearray, memoryview)):
        payload = bytes(content)
    elif isinstance(content, str):
        payload = content.encode("utf-8")
    else:
        raise TypeError(
            f"unsupported element.content type: {type(content).__name__}",
        )
    shell.atomic_write_binary(rel, io.BytesIO(payload))
