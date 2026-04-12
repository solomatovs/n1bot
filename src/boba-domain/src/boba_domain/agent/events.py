"""События doc-пайплайна и их сериализация."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Union

from boba_domain.search.types import Fragment, SearchHit

# ---------------------------------------------------------------------------
# Общие события
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StageStarted:
    """Этап начал выполнение."""

    stage: str


@dataclass(frozen=True)
class StageCompleted:
    """Этап завершил выполнение."""

    stage: str
    detail: str


# ---------------------------------------------------------------------------
# Индексация
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IndexingSkipped:
    """Индекс уже существует — пропускаем."""

    collection: str
    doc_count: int


@dataclass(frozen=True)
class FileIndexed:
    """Файл проиндексирован."""

    filename: str
    chunks: int
    index: int
    total: int


@dataclass(frozen=True)
class IndexingDone:
    """Индексация завершена."""

    total_files: int
    total_chunks: int


# ---------------------------------------------------------------------------
# Поиск
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SearchDone:
    """Поиск завершён — найдены чанки."""

    hits: List[SearchHit] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Чтение контекста
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextReady:
    """Расширенный контекст собран из файлов."""

    context: str
    fragments: List[Fragment] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Генерация
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ThinkingToken:
    """Токен размышления (стриминг)."""

    token: str


@dataclass(frozen=True)
class AnswerToken:
    """Токен ответа (стриминг)."""

    token: str


@dataclass(frozen=True)
class GenerationDone:
    """Генерация завершена."""


# ---------------------------------------------------------------------------
# Tool calls
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCallStarted:
    """LLM вызывает инструмент."""

    tool_call_id: str
    tool_name: str
    arguments: str


@dataclass(frozen=True)
class ToolResultReady:
    """Инструмент вернул результат."""

    tool_call_id: str
    tool_name: str
    content: str
    is_error: bool = False


# ---------------------------------------------------------------------------
# Объединённый тип
# ---------------------------------------------------------------------------

DocPipelineEvent = Union[
    StageStarted,
    StageCompleted,
    IndexingSkipped,
    FileIndexed,
    IndexingDone,
    SearchDone,
    ContextReady,
    ThinkingToken,
    AnswerToken,
    GenerationDone,
    ToolCallStarted,
    ToolResultReady,
]


# ---------------------------------------------------------------------------
# Сериализация
# ---------------------------------------------------------------------------


class DocPipelineEventSerializer:
    """Сериализация DocPipelineEvent → dict для SSE/JSON."""

    class _Type:
        STAGE_STARTED = "stage_started"
        STAGE_COMPLETED = "stage_completed"
        INDEXING_SKIPPED = "indexing_skipped"
        FILE_INDEXED = "file_indexed"
        INDEXING_DONE = "indexing_done"
        SEARCH_DONE = "search_done"
        CONTEXT_READY = "context_ready"
        THINKING = "thinking"
        ANSWER = "answer"
        DONE = "done"
        TOOL_CALL = "tool_call"
        TOOL_RESULT = "tool_result"

    @classmethod
    def to_dict(cls, event: DocPipelineEvent) -> Dict[str, Any]:
        T = cls._Type
        match event:
            case StageStarted(stage=stage):
                return {"type": T.STAGE_STARTED, "stage": stage}
            case StageCompleted(stage=stage, detail=detail):
                return {"type": T.STAGE_COMPLETED, "stage": stage, "detail": detail}
            case IndexingSkipped(collection=coll, doc_count=count):
                return {
                    "type": T.INDEXING_SKIPPED,
                    "collection": coll,
                    "doc_count": count,
                }
            case FileIndexed(filename=name, chunks=chunks, index=idx, total=total):
                return {
                    "type": T.FILE_INDEXED,
                    "filename": name,
                    "chunks": chunks,
                    "index": idx,
                    "total": total,
                }
            case IndexingDone(total_files=files, total_chunks=chunks):
                return {
                    "type": T.INDEXING_DONE,
                    "total_files": files,
                    "total_chunks": chunks,
                }
            case SearchDone(hits=hits):
                return {"type": T.SEARCH_DONE, "hits_count": len(hits)}
            case ContextReady(context=ctx, fragments=frags):
                return {
                    "type": T.CONTEXT_READY,
                    "length": len(ctx),
                    "fragments_count": len(frags),
                }
            case ThinkingToken(token=tok):
                return {"type": T.THINKING, "token": tok}
            case AnswerToken(token=tok):
                return {"type": T.ANSWER, "token": tok}
            case GenerationDone():
                return {"type": T.DONE}
            case ToolCallStarted(tool_call_id=tid, tool_name=name, arguments=args):
                return {
                    "type": T.TOOL_CALL,
                    "tool_call_id": tid,
                    "name": name,
                    "arguments": args,
                }
            case ToolResultReady(
                tool_call_id=tid, tool_name=name, content=content, is_error=err
            ):
                return {
                    "type": T.TOOL_RESULT,
                    "tool_call_id": tid,
                    "name": name,
                    "content": content,
                    "is_error": err,
                }
