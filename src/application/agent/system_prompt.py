"""SystemPromptFiller — загрузка system prompt из файла.

PipelineStage: читает system_prompt.md из .boba/, добавляет в window.
Создаёт файл по умолчанию если не существует.
"""
from __future__ import annotations

from typing import Iterator

from domain.agent.context_filler import ContextRequest
from domain.agent.events import DocPipelineEvent, StageCompleted, StageStarted
from domain.core.pipeline import PipelineStage
from domain.workspace import Workspace

_FILENAME = "system_prompt.md"

_DEFAULT_CONTENT = """\
Ты — эксперт по документации. У тебя есть инструменты для работы с документами.

Стратегия работы:
1. Если пользователь ссылается на предыдущий разговор — получи историю чата
2. Проиндексируй документы перед первым поиском
3. Ищи релевантные фрагменты, можно несколько раз с разными запросами
4. Читай файлы для получения полного контекста
5. Когда достаточно информации — дай ответ

Правила:
- Отвечай ТОЛЬКО на основе найденной информации из документов
- Если документы не содержат ответа, скажи об этом прямо
- Указывай источники (файл и строки) в ответе
"""


class SystemPromptFiller(PipelineStage[ContextRequest, DocPipelineEvent]):
    """PipelineStage: загружает system prompt из файла в window."""

    def __init__(self, workspace: Workspace) -> None:
        self._path = workspace.boba_path / _FILENAME

    @property
    def name(self) -> str:
        return "system_prompt"

    def run(self, ctx: ContextRequest) -> Iterator[DocPipelineEvent]:
        yield StageStarted(stage=self.name)
        if not self._path.exists():
            self._path.write_text(_DEFAULT_CONTENT, encoding="utf-8")
        content = self._path.read_text(encoding="utf-8").strip()
        ctx.window.add_system(content)
        yield StageCompleted(stage=self.name, detail=f"{len(content)} символов")
