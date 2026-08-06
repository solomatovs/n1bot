"""Ingest-payload внутри песочницы: там его и запускает инструмент.

Прогон целиком требует Confluence и postgres, поэтому проверяется граница —
что модуль догружается в песочнице со всеми зависимостями конвейера и отвечает
по контракту, а не падает импортом.
"""

from __future__ import annotations

import pytest
from conftest import needs_sandbox, needs_userns, sandbox_profile
from pydantic import BaseModel

from boba.sandbox import SandboxCaller, SandboxToolConfig
from boba.tool.kb.confluence.ingest_caller import IngestRequest
from boba.toolkit.launcher import LauncherError, NoChunks

ENTRY = ("python3", "-m", "boba.tool.kb.confluence.ingest_payload")


class _Answer(BaseModel):
    stats: dict


def _caller() -> SandboxCaller:
    sandbox = SandboxToolConfig.model_validate(
        {"profile": sandbox_profile(), "override": {}}
    )
    return SandboxCaller("ingest-test", sandbox.effective(), dict)


@needs_sandbox
@needs_userns
def test_payload_loads_and_validates_config() -> None:
    """Пустой конфиг: важно, что ответ — про поля запроса, а не про импорт."""
    request = IngestRequest(
        op=IngestRequest.OP,
        config={},
        mode="pages",
        prune_missing=False,
        force_update=False,
    )

    with pytest.raises(LauncherError, match="ValidationError") as exc:
        _caller().call_stream(ENTRY, request, NoChunks(), _Answer)

    reason = str(exc.value)
    assert "ModuleNotFoundError" not in reason
    assert "ImportError" not in reason
    # параллелизм страниц задаётся явно — без него конфиг невалиден
    assert "page_workers" in reason
