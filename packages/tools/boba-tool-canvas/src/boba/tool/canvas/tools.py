"""Tools канваса: файлы workspace треда для показа, вложения и диаграмм.

Тела работают с файлами в смонтированном /workspace и возвращают результат
по форме данных: CanvasResult — файл для панели, FileResult — вложение.
Показ в панели, карточка в переписке и вердикт браузера — дело поверхности
чата, которая монтирует элементы по chat_view() результата; тела о ней не
знают.

Ошибки:
своих не выпускает: отказ (путь вне треда, нет файла, плохая спека) —
    ErrorResult с error_kind.
"""

from __future__ import annotations

import mimetypes
from enum import StrEnum
from pathlib import Path
from typing import Annotated, ClassVar, Final

from pydantic import BaseModel, ConfigDict, Field

from boba.canvas.diagram import (
    DiagramErrorKind,
    DiagramMarker,
    DiagramPrompt,
    DiagramSpecError,
    MermaidSpec,
)
from boba.canvas.keys import ObjectKey, ThreadDir, WorkspaceRoot
from boba.identity.context import Scope, Subject
from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import Injected, tool
from boba.toolkit.result import (
    CanvasResult,
    ErrorResult,
    FileResult,
    MarkdownResult,
)

__all__ = [
    "TOOLS",
    "CanvasErrorKind",
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


class CanvasErrorKind(StrEnum):
    """Коды отказов тулов канваса: уезжают в ErrorResult.error_kind."""

    BAD_PATH = "bad_path"
    FILE_NOT_FOUND = "file_not_found"


class CanvasPrompt(StrEnum):
    """Тексты фасада: описания параметров и оговорки для LLM."""

    PATH = (
        "Путь к файлу в workspace треда: '/workspace/<thread_id>/mermaid/<имя>.mmd' "
        "или '/workspace/<thread_id>/upload/<имя>'. Файл должен существовать. "
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
        "Путь к файлу в каталогах треда: '/workspace/<thread_id>/upload/<имя>' "
        "или '/workspace/<thread_id>/mermaid/<имя>'. Файл из другого места "
        "workspace сначала перенеси туда через bash."
    )


class CanvasRefusedError(Exception):
    """Тул отработать не может; kind и текст причины готовы для ErrorResult."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind

    def result(self) -> ErrorResult:
        return ErrorResult(message=str(self), error_kind=self.kind)


class ThreadFiles:
    """Файлы треда в смонтированном workspace: ключ по пути и проверка наличия."""

    FALLBACK_MIME: ClassVar[str] = "application/octet-stream"

    def __init__(self, subject: Subject, scope: Scope, root: WorkspaceRoot) -> None:
        root.apply()
        self._user_id = subject.user_key
        self._thread_id = scope.id

    def existing(self, path: str) -> ObjectKey:
        """Ключ файла по пути из аргумента; вне каталогов треда или без
        файла на диске — отказ."""
        key = self.key(path)
        if not Path(key.in_workspace()).is_file():
            raise CanvasRefusedError(
                CanvasErrorKind.FILE_NOT_FOUND, f"file not found: {key.in_workspace()}"
            )

        return key

    def key(self, path: str) -> ObjectKey:
        try:
            return ObjectKey.from_workspace(self._user_id, self._thread_id, path)
        except ValueError as e:
            raise CanvasRefusedError(CanvasErrorKind.BAD_PATH, str(e)) from e

    def diagram(self, name: str) -> ObjectKey:
        """Ключ спеки в каталоге mermaid/ треда; имя приводится к безопасному."""
        return ObjectKey.build(
            self._user_id,
            self._thread_id,
            name,
            DiagramMarker.FALLBACK_NAME,
            dir_thread=ThreadDir.MERMAID,
        )

    @classmethod
    def mime_of(cls, key: ObjectKey) -> str:
        guessed = mimetypes.guess_type(key.name)[0]
        if not guessed:
            return cls.FALLBACK_MIME

        return guessed


class DiagramSpec:
    """Спека mermaid из аргумента: предел длины и разбор."""

    @staticmethod
    def parse(spec: str, max_chars: int) -> MermaidSpec:
        if len(spec) > max_chars:
            msg = (
                f"the spec is longer than the {max_chars} character limit: "
                f"got {len(spec)} characters"
            )
            raise CanvasRefusedError(DiagramErrorKind.INVALID_SPEC, msg)

        try:
            return MermaidSpec.parse(spec)
        except DiagramSpecError as e:
            raise CanvasRefusedError(DiagramErrorKind.INVALID_SPEC, str(e)) from e


@tool
async def canvas_open(
    path: Annotated[str, Field(min_length=1, description=CanvasPrompt.PATH)],
    subject: Annotated[Subject, Injected],
    scope: Annotated[Scope, Injected],
    root: Annotated[WorkspaceRoot, Injected],
) -> CanvasResult | ErrorResult:
    """Показать файл workspace (диаграмму, изображение, pdf, текст) в
    панели справа от чата и оставить ссылку на него в переписке."""
    try:
        key = ThreadFiles(subject, scope, root).existing(path)
    except CanvasRefusedError as e:
        return e.result()

    return CanvasResult(
        path=key.in_workspace(), label=key.name, note=CanvasPrompt.OPENED_NOTE
    )


@tool
async def send_file(
    path: Annotated[str, Field(min_length=1, description=CanvasPrompt.FILE_PATH)],
    subject: Annotated[Subject, Injected],
    scope: Annotated[Scope, Injected],
    root: Annotated[WorkspaceRoot, Injected],
) -> FileResult | ErrorResult:
    """Отправить пользователю файл из workspace вложением в чат."""
    try:
        key = ThreadFiles(subject, scope, root).existing(path)
    except CanvasRefusedError as e:
        return e.result()

    return FileResult(
        path=key.in_workspace(), name=key.name, mime=ThreadFiles.mime_of(key)
    )


@tool
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
    try:
        parsed = DiagramSpec.parse(spec, cfg.max_chars)
    except CanvasRefusedError as e:
        return e.result()

    key = ThreadFiles(subject, scope, root).diagram(name)
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

if __name__ == "__main__":
    raise SystemExit(ToolMain.run(TOOLS))
