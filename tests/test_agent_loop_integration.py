from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

from boba.adapters.console_sink import ConsoleSink
from boba.adapters.fs_workspace import (
    FsExtensionWorkspaceRegistry,
    FsHistoryWorkspaceRegistry,
    FsProjectWorkspaceRegistry,
)
from boba.adapters.in_memory_messages import InMemoryMessageService
from boba.adapters.raw_llm_observer import CompositeRawLLMObserver, FileContentObserver
from boba.domain.agent.models import AgentRequest
from boba.domain.core.tools import ToolContext
from boba.domain.core.workspace import (
    ExtensionWorkspaceId,
    WorkspaceId,
)
from boba.domain.llm.models import RequestId
from boba.infra.config import ConfigLoader, SamplingLoader
from boba.infra.container import (
    AgentComponents,
    build_prompt_providers,
    create_agent,
)
from boba.infra.extensions import ExtensionContext, ExtensionLoader
from boba.infra.logging import configure_logging

pytestmark = pytest.mark.integration


def _run(query: str, model: str) -> None:
    """Собирает агент с полным стеком middleware и прогоняет один запрос."""
    loader = ConfigLoader()
    app_config = loader.load_app()
    agent_config = loader.load_agent()
    sampling = SamplingLoader().load()
    configure_logging(app_config.log_level, app_config.log_file)

    workspace_id = WorkspaceId.from_wire("00000000-0000-0000-0000-000000000001")

    extension_workspace = FsExtensionWorkspaceRegistry(
        root=Path(app_config.extensions_dir),
    ).get_or_create(ExtensionWorkspaceId("extensions"))

    extension_loader = ExtensionLoader(
        extension_workspace,
        ExtensionContext(
            extension_workspace=extension_workspace,
            app_config=app_config,
            agent_config=agent_config,
            sampling=sampling,
        ),
    )

    project_workspace = FsProjectWorkspaceRegistry(
        base_dir=Path(app_config.workspaces.base_dir),
        subdir=app_config.workspaces.user_subdir,
    ).get_or_create(workspace_id)

    history_workspace = FsHistoryWorkspaceRegistry(
        base_dir=Path(app_config.workspaces.base_dir),
        subdir=app_config.workspaces.system_subdir,
    ).get_or_create(workspace_id)

    agent = create_agent(
        llm_config=app_config.llm,
        components=AgentComponents(
            agent_config=agent_config,
            sampling=sampling,
            prompt_providers=build_prompt_providers(extension_loader),
            message_service=InMemoryMessageService(),
            tools_service=extension_loader.tools_service(),
        ),
        tool_ctx=ToolContext(project_workspace=project_workspace),
        observer=CompositeRawLLMObserver(
            [
                FileContentObserver(history_workspace),
            ]
        ),
        sink=ConsoleSink(sys.stdout, sys.stderr),
    )

    request = AgentRequest(
        model=model,
        request_id=RequestId.new(),
    )

    agent.run(agent_config, request, query)


def test_agent_loop_hello(query: str, model: str) -> None:
    _run(query, model)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        required=True,
        help="Имя модели — обязательно (системного дефолта нет)",
    )
    parser.add_argument("query", nargs="+", help="Сообщение пользователя")
    args = parser.parse_args()
    _run(" ".join(args.query), args.model)
