"""Tool: перезаписать файл целиком."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from boba.tool.files.enable import files_enable_if
from boba.tools import FromDI, Scope, tool
from boba.workspace.contract import ProjectWorkspaceShell, WorkspaceError

__all__ = ["WriteTool"]


@tool(enable_if=files_enable_if("write"))
class WriteTool:
    """Полностью перезаписать файл содержимым.

    Если файла или промежуточных директорий нет — создать.
    """

    def __call__(
        self,
        path: Annotated[str, Field(min_length=1, description="Путь к файлу.")],
        content: Annotated[str, Field(description="Новое содержимое файла.")],
        shell: Annotated[ProjectWorkspaceShell, FromDI(Scope.APP)],
        encoding: Annotated[
            str,
            Field(min_length=1, description="Кодировка файла. По умолчанию 'utf-8'."),
        ] = "utf-8",
    ) -> str:
        existed = shell.exists(path)
        try:
            with shell.write_text(path, encoding) as f:
                f.write(content)
        except WorkspaceError as e:
            raise RuntimeError(f"Ошибка записи: {e}") from e
        action = "обновлён" if existed else "создан"
        return f"Файл {action}: {path} ({len(content)} символов)"
