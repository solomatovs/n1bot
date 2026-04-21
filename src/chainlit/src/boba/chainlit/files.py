"""Сохранение загруженных файлов в project-workspace.

Файлы кладутся в корень workspace — туда же, куда смотрит ``ls`` без
аргументов у file-tools агента. Повторная загрузка с тем же именем
перезаписывает файл — это штатный re-upload, без отдельного API.
Удаление/листинг делегируются агенту через те же tools.
"""

from __future__ import annotations

import shutil

from boba.domain.core.workspace import ProjectWorkspaceShell


def save_upload(
    shell: ProjectWorkspaceShell, src_path: str, name: str
) -> str:
    """Сохранить файл ``src_path`` как ``<name>`` в корне workspace.

    ``src_path`` — абсолютный путь temp-файла, куда Chainlit сложил
    аплоад. Имя санитайзится: оставляем только basename и запрещаем
    ``.``/``..`` — даже если UI прислал что-то странное, в корень
    workspace попадёт только одиночный безопасный сегмент.

    Возвращает relative path внутри workspace (``<name>``).
    """
    safe = name.replace("\\", "/").split("/")[-1].strip()
    if not safe or safe in {".", ".."}:
        msg = f"invalid upload name: {name!r}"
        raise ValueError(msg)
    with open(src_path, "rb") as src, shell.write_binary(safe) as dst:
        shutil.copyfileobj(src, dst)
    return safe
