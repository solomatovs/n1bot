"""FastAPI dependencies — доступ к DI-контейнеру."""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

from dishka import Container
from fastapi import Depends, Request

from boba_domain.di_types import FolderContext


def get_container(request: Request) -> Container:
    return request.app.state.container


def get_scope(
    folder_path: Path,
    container: Container = Depends(get_container),
    history_path: Path | None = None,
) -> Iterator[Container]:
    """REQUEST scope для конкретной папки."""
    ctx = FolderContext(folder_path=folder_path, history_path=history_path)
    with container(context={FolderContext: ctx}) as scope:
        yield scope
