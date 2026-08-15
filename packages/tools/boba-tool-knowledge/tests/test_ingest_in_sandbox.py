"""Ingest-модуль внутри песочницы: там его и запускает инструмент.

Прогон целиком требует Confluence и postgres, поэтому проверяется граница —
что модуль догружается в песочнице со всеми зависимостями конвейера и отвечает
по контракту запуска, а не падает импортом.
"""

from __future__ import annotations

from conftest import needs_sandbox, needs_userns, sandbox_profile

from boba.sandbox import SandboxCaller, SandboxToolConfig
from boba.toolkit.entry import ReplyError, ToolCommand


def _caller() -> SandboxCaller:
    sandbox = SandboxToolConfig.model_validate(
        {"profile": sandbox_profile(), "override": {}}
    )
    return SandboxCaller("ingest-test", sandbox.effective(), dict)


@needs_sandbox
@needs_userns
def test_module_loads_and_validates_config() -> None:
    """Пустой конфиг: важно, что ответ — про поля конфига, а не про импорт."""
    command = ToolCommand(
        argv=(
            "python3",
            "-m",
            "boba.tool.kb.confluence.ingest_tools",
            "confluence_index_pages",
            "--page-ids",
            '["1"]',
        ),
        stdin=b'{"cfg": {}}',
    )

    outcome = _caller().run_tool(command)

    reply = outcome.reply
    assert isinstance(reply, ReplyError)
    assert reply.kind == "invalid_request"
    assert "ModuleNotFoundError" not in reply.message
    assert "ImportError" not in reply.message
    # параллелизм страниц задаётся явно — без него конфиг невалиден
    assert "page_workers" in reply.message
