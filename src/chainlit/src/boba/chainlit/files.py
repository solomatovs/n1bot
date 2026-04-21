"""Сохранение загруженных файлов в project-workspace.

Файлы складываются в ``uploads/`` — ту же директорию, что видят
file-tools агента (``ls``, ``cat``, ``grep``). Повторная загрузка с тем
же именем перезаписывает файл — это штатный re-upload, без отдельного
API. Удаление/листинг делегируются агенту через те же tools.
"""

from __future__ import annotations

import shutil

from boba.domain.core.workspace import ProjectWorkspaceShell

UPLOADS_DIR = "uploads"


def save_upload(
    shell: ProjectWorkspaceShell, src_path: str, name: str
) -> str:
    """Сохранить файл ``src_path`` как ``uploads/<name>`` в workspace.

    ``src_path`` — абсолютный путь temp-файла, куда Chainlit сложил
    аплоад. Имя санитайзится: оставляем только basename и запрещаем
    ``.``/``..`` — даже если UI прислал что-то странное, внутрь
    ``uploads/`` попадёт только одиночный безопасный сегмент.

    Возвращает relative path внутри workspace (``uploads/<name>``).
    """
    safe = name.replace("\\", "/").split("/")[-1].strip()
    if not safe or safe in {".", ".."}:
        msg = f"invalid upload name: {name!r}"
        raise ValueError(msg)
    target = f"{UPLOADS_DIR}/{safe}"
    with open(src_path, "rb") as src, shell.write_binary(target) as dst:
        shutil.copyfileobj(src, dst)
    return target
