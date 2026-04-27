"""Сохранение загруженных файлов в project-workspace."""

from __future__ import annotations

import shutil

from boba.domain.core.workspace import ProjectWorkspaceShell


def save_upload(shell: ProjectWorkspaceShell, src_path: str, name: str) -> str:
    """Сохранить src_path как <name> в корне workspace; имя санитайзится до basename."""
    safe = name.replace("\\", "/").split("/")[-1].strip()
    if not safe or safe in {".", ".."}:
        msg = f"invalid upload name: {name!r}"
        raise ValueError(msg)
    with open(src_path, "rb") as src, shell.write_binary(safe) as dst:
        shutil.copyfileobj(src, dst)
    return safe
