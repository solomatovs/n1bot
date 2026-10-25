"""Tools канваса: файлы workspace области для показа, вложения и диаграмм.

Тела работают с файлами в смонтированном /workspace и возвращают результат
по форме данных: CanvasResult — файл для панели, FileResult — вложение.
Клиенту оба уходят ссылкой resource_link на файл; показ в панели, карточка
в переписке и вердикт браузера — дело поверхности чата, тела о ней не
знают.

Ошибки:
своих не выпускает: отказ (путь вне области, нет файла, плохая спека) —
    ErrorResult с error_kind.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, ClassVar, Final

from pydantic import BaseModel, ConfigDict, Field

from boba.canvas.canvas import CanvasError, CanvasErrorKind, CanvasScope
from boba.canvas.diagram import (
    DiagramMarker,
    DiagramPrompt,
    DiagramSpecError,
    MermaidSpec,
)
from boba.canvas.keys import ObjectKey, ScopeDir, WorkspaceRoot
from boba.identity.context import Scope, Subject
from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import Injected, tool
from boba.toolkit.result import (
    CanvasResult,
    ContentMime,
    ErrorResult,
    FileResult,
    MarkdownResult,
)

__all__ = [
    "TOOLS",
    "CanvasPrompt",
    "CanvasToolConfig",
    "canvas_open",
    "diagram_save",
    "send_file",
]


class CanvasToolConfig(BaseModel):
    """Секция [tool.canvas]: предел размера спеки диаграммы."""

    model_config = ConfigDict(extra="ignore")

    SECTION: ClassVar[str] = "tool.canvas"

    max_chars: int = Field(ge=1, description="Потолок длины спеки mermaid.")


class CanvasPrompt(StrEnum):
    """Тексты фасада: описания параметров и оговорки для LLM."""

    PATH = (
        "Путь к файлу в workspace области: '/workspace/<scope_id>/mermaid/<имя>.mmd' "
        "или '/workspace/<scope_id>/upload/<имя>'. Файл должен существовать. "
        "Поддерживаются диаграммы mermaid (.mmd), изображения "
        "(.png/.jpg/.jpeg/.gif/.svg/.webp), .pdf, текст (.txt/.md/.log), "
        "видео (.mp4/.webm/.mov) и аудио (.mp3/.wav/.ogg/.m4a/.flac) — "
        "сгенерируй файл любым инструментом и покажи его здесь."
    )
    OPENED_NOTE = (
        "the panel is open for the user and a link to it stays in the chat; "
        "mermaid files are rendered by the browser and a render failure "
        "comes back to you as a tool error"
    )
    FILE_PATH = (
        "Путь к файлу в каталогах области: '/workspace/<scope_id>/upload/<имя>' "
        "или '/workspace/<scope_id>/mermaid/<имя>'. Файл из другого места "
        "workspace сначала перенеси туда через bash."
    )


class ScopeFiles:
    """Файлы области в смонтированном workspace для тел канваса: ключ по пути
    аргумента в области CanvasScope и проверка, что файл лежит на диске."""

    def __init__(self, subject: Subject, scope: Scope, root: WorkspaceRoot) -> None:
        root.apply()
        self._scope = CanvasScope(
            user_id=subject.user_key, scope_id=scope.id, workspace=root.path
        )

    def existing(self, path: str) -> ObjectKey:
        """Ключ файла по пути из аргумента.

        Ошибки:
        CanvasError — путь вне каталогов области (BAD_PATH) или файла нет на
            диске (FILE_NOT_FOUND).
        """
        key = self._scope.key(path)
        if not Path(key.in_workspace()).is_file():
            raise CanvasError(
                CanvasErrorKind.FILE_NOT_FOUND, f"file not found: {key.in_workspace()}"
            )

        return key

    def diagram(self, name: str) -> ObjectKey:
        """Ключ спеки в каталоге mermaid/ области; имя приводится к безопасному."""
        return ObjectKey.build(
            self._scope.user_id,
            self._scope.scope_id,
            name,
            DiagramMarker.FALLBACK_NAME,
            scope_dir=ScopeDir.MERMAID,
        )


@tool(destructive=False)
async def canvas_open(
    path: Annotated[str, Field(min_length=1, description=CanvasPrompt.PATH)],
    subject: Annotated[Subject, Injected],
    scope: Annotated[Scope, Injected],
    root: Annotated[WorkspaceRoot, Injected],
) -> CanvasResult | ErrorResult:
    """Показать файл workspace (диаграмму, изображение, pdf, текст) в
    панели справа от чата и оставить ссылку на него в переписке."""
    try:
        key = ScopeFiles(subject, scope, root).existing(path)
    except CanvasError as e:
        return e.failure()

    return CanvasResult(
        path=key.in_workspace(), label=key.name, note=CanvasPrompt.OPENED_NOTE
    )


@tool(destructive=False)
async def send_file(
    path: Annotated[str, Field(min_length=1, description=CanvasPrompt.FILE_PATH)],
    subject: Annotated[Subject, Injected],
    scope: Annotated[Scope, Injected],
    root: Annotated[WorkspaceRoot, Injected],
) -> FileResult | ErrorResult:
    """Отправить пользователю файл из workspace вложением в чат."""
    try:
        key = ScopeFiles(subject, scope, root).existing(path)
    except CanvasError as e:
        return e.failure()

    return FileResult(
        path=key.in_workspace(), name=key.name, mime=ContentMime.of_name(key.name)
    )


@tool(destructive=False)
async def diagram_save(  # noqa: PLR0913 — контекст вызова и конфиг едут отдельными параметрами
    name: Annotated[str, Field(min_length=1, description=DiagramPrompt.NAME)],
    spec: Annotated[
        str,
        Field(min_length=1, description=DiagramPrompt.SPEC),
        MarkdownResult(language="mermaid"),
    ],
    subject: Annotated[Subject, Injected],
    scope: Annotated[Scope, Injected],
    root: Annotated[WorkspaceRoot, Injected],
    cfg: Annotated[CanvasToolConfig, Injected],
) -> CanvasResult | ErrorResult:
    """Сохранить спеку mermaid файлом в workspace, показать её в панели
    канваса и оставить карточку в переписке.

    Спеку проверяет только mermaid.js в браузере при показе, поэтому показ
    и есть проверка: его вердикт возвращается как ошибка инструмента, и
    тогда спеку правят следующим вызовом.
    """
    if len(spec) > cfg.max_chars:
        msg = (
            f"the spec is longer than the {cfg.max_chars} character limit: "
            f"got {len(spec)} characters"
        )
        return ErrorResult(message=msg, error_kind=CanvasErrorKind.INVALID_SPEC)

    try:
        parsed = MermaidSpec.parse(spec)
    except DiagramSpecError as e:
        return ErrorResult(message=str(e), error_kind=CanvasErrorKind.INVALID_SPEC)

    key = ScopeFiles(subject, scope, root).diagram(name)
    target = Path(key.in_workspace())
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(parsed.text, encoding=DiagramMarker.ENCODING)

    path = key.in_workspace()

    return CanvasResult(
        path=path,
        label=key.name,
        summary=f"diagram saved: {path}",
        note=DiagramPrompt.SAVED_NOTE,
    )


TOOLS: Final = ToolMain.toolset(canvas_open, send_file, diagram_save)
