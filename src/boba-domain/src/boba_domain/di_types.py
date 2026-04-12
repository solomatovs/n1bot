"""Типы для dependency injection.

NewType'ы для примитивных зависимостей, которые dishka
не может различить по базовому типу (str, int).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import NewType

# Имя коллекции в vectorstore (производное от имени папки)
CollectionName = NewType("CollectionName", str)

# Имя модели эмбеддингов
EmbeddingModel = NewType("EmbeddingModel", str)


@dataclass(frozen=True)
class WorkspaceContext:
    """Runtime-параметры для REQUEST scope.

    Передаётся при входе в scope — определяет рабочую папку.
    """

    folder_path: Path
