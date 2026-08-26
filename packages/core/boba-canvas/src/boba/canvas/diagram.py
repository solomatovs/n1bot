"""Диаграммы mermaid: разбор спецификации, маркеры и тексты фасада, запись диаграммы.

Ошибки:
DiagramSpecError — спецификация не разобрана.
DiagramRefusedError — диаграмма отклонена; код — DiagramErrorKind.
"""

from __future__ import annotations

import textwrap
from enum import StrEnum
from typing import ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field

from boba.canvas.keys import ObjectKey
from boba.identity.errors import RefusalError

__all__ = [
    "DiagramEntry",
    "DiagramErrorKind",
    "DiagramMarker",
    "DiagramPrompt",
    "DiagramRefusedError",
    "DiagramSpecError",
    "DiagramToolConfig",
    "MermaidSpec",
    "MermaidToken",
]


class DiagramSpecError(ValueError):
    """Спека пуста или не начинается с известного типа диаграммы mermaid."""


class DiagramErrorKind(StrEnum):
    """Коды отказов тулов диаграмм: уезжают в ErrorResult.error_kind."""

    INVALID_SPEC = "invalid_diagram_spec"
    BAD_PATH = "bad_path"
    FILE_NOT_FOUND = "file_not_found"
    STORAGE_ERROR = "storage_error"
    BAD_FILE = "bad_file"


class DiagramRefusedError(RefusalError):
    """Тул отработать не может; текст причины готов для LLM."""


class DiagramToolConfig(BaseModel):
    """Секция [tool.diagram]: предел размера спеки."""

    model_config = ConfigDict(extra="ignore")

    max_chars: int = Field(ge=1)


class MermaidToken(StrEnum):
    """Маркеры текста спеки, которые понимает нормализация."""

    FENCE = "```"
    FRONTMATTER = "---"
    COMMENT = "%%"
    TITLE_KEY = "title:"


class DiagramMarker(StrEnum):
    """Служебные имена: jsx-компонент, mime файла, канвас, fallback-имя."""

    MIME = "text/plain"
    FALLBACK_NAME = "diagram.mmd"
    SUFFIX = ".mmd"


class DiagramPrompt(StrEnum):
    """Тексты фасада для LLM: описания параметров и оговорка о рендере."""

    NAME = (
        "Имя файла диаграммы с расширением .mmd, например 'orders.mmd'. "
        "Файл ляжет в '/workspace/<thread_id>/mermaid/'."
    )
    SPEC = (
        "Спека mermaid. Первая строка — тип диаграммы: erDiagram, flowchart, "
        "sequenceDiagram, stateDiagram, gantt, mindmap и другие. Направление "
        "задаётся строкой 'direction LR' внутри спеки. Большую схему дроби на "
        "несколько диаграмм. Синтаксис тела проверяет браузер при показе, "
        "поэтому пиши строго: подпись подграфа без пробелов внутри скобок — "
        "'subgraph ID[\"Текст\"]', а не 'subgraph ID[ \"Текст\" ]'. "
        "sankey-beta принимает в подписях узлов только латиницу — для русских "
        "подписей бери другой тип (flowchart, xychart-beta)."
    )
    SAVED_NOTE = (
        "the diagram is rendered in the canvas panel and its card is shown "
        "in the chat. A render failure comes back to you as a tool error."
    )


class MermaidSpec(BaseModel):
    """Нормализованная спека mermaid: текст, тип диаграммы, заголовок."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    diagram_type: str
    title: str | None

    TYPES: ClassVar[frozenset[str]] = frozenset(
        {
            "flowchart",
            "graph",
            "sequenceDiagram",
            "classDiagram",
            "stateDiagram",
            "stateDiagram-v2",
            "erDiagram",
            "journey",
            "gantt",
            "pie",
            "quadrantChart",
            "requirementDiagram",
            "gitGraph",
            "C4Context",
            "C4Container",
            "C4Component",
            "C4Dynamic",
            "C4Deployment",
            "mindmap",
            "timeline",
            "kanban",
            "block-beta",
            "packet-beta",
            "sankey-beta",
            "xychart-beta",
            "architecture-beta",
            "radar-beta",
            "treemap-beta",
        }
    )
    """Типы диаграмм mermaid 11; пересматривается при обновлении версии в Makefile."""

    @classmethod
    def parse(cls, raw: str) -> Self:
        """Разобрать спеку; неразбираемый вход — DiagramSpecError."""
        text = cls._strip_fence(raw)

        if not text:
            raise DiagramSpecError("the spec is empty")

        title, body = cls._split_frontmatter(text)

        head = cls._first_token(body)
        if head is None:
            raise DiagramSpecError("the spec has no meaningful lines")

        if head not in cls.TYPES:
            known = ", ".join(sorted(cls.TYPES))
            raise DiagramSpecError(
                f"the first line must name a mermaid diagram type, "
                f"got {head!r}; supported: {known}"
            )

        return cls(text=text, diagram_type=head, title=title)

    @classmethod
    def _strip_fence(cls, raw: str) -> str:
        text = textwrap.dedent(raw).strip()

        lines = text.splitlines()
        if lines and lines[0].startswith(MermaidToken.FENCE):
            lines = lines[1:]
        if lines and lines[-1].strip() == MermaidToken.FENCE:
            lines = lines[:-1]

        return textwrap.dedent("\n".join(lines)).strip()

    @classmethod
    def _split_frontmatter(cls, text: str) -> tuple[str | None, str]:
        """Заголовок из YAML-frontmatter и тело после него; сам блок не вырезается."""
        lines = text.splitlines()

        if not lines:
            return None, text

        if lines[0].strip() != MermaidToken.FRONTMATTER:
            return None, text

        title: str | None = None
        for index, line in enumerate(lines[1:], start=1):
            stripped = line.strip()
            if stripped == MermaidToken.FRONTMATTER:
                body = "\n".join(lines[index + 1 :])
                return title, body
            if stripped.startswith(MermaidToken.TITLE_KEY):
                title = cls._clean_title(stripped[len(MermaidToken.TITLE_KEY) :])

        return None, text

    @staticmethod
    def _clean_title(raw: str) -> str | None:
        cleaned = raw.strip().strip("'\"").strip()
        if not cleaned:
            return None
        return cleaned

    @classmethod
    def _first_token(cls, body: str) -> str | None:
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith(MermaidToken.COMMENT):
                continue
            return stripped.split()[0]
        return None


class DiagramEntry(BaseModel):
    """Одна диаграмма для фронта: путь, подпись и текст спеки как есть."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    name: str
    label: str
    """Подпись в селекторе: title из спеки, иначе имя файла."""
    spec: str
    type: str
    """Тип диаграммы; пустой — спека сейчас не парсится."""

    @classmethod
    def of(cls, key: ObjectKey, text: str) -> Self:
        """Метаданные берутся из спеки; неразбираемая спека едет без них."""
        label = key.name
        diagram_type = ""

        try:
            parsed = MermaidSpec.parse(text)
        except DiagramSpecError:
            return cls(
                path=key.in_workspace(),
                name=key.name,
                label=label,
                spec=text,
                type=diagram_type,
            )

        if parsed.title:
            label = parsed.title

        return cls(
            path=key.in_workspace(),
            name=key.name,
            label=label,
            spec=parsed.text,
            type=parsed.diagram_type,
        )
