"""FastAPI dependencies — доступ к DI-контейнеру."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from dishka import Container
from fastapi import Depends, Request

from boba_domain.di_types import WorkspaceContext


def get_container(request: Request) -> Container:
    return request.app.state.container


def get_scope(
    folder_path: Path,
    container: Container = Depends(get_container),
) -> Iterator[Container]:
    """REQUEST scope для конкретной папки."""
    ctx = WorkspaceContext(folder_path=folder_path)
    with container(context={WorkspaceContext: ctx}) as scope:
        yield scope
