from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from boba.adapters.fs_workspace import (
    FsPluginWorkspaceRegistry,
    FsProjectWorkspaceRegistry,
    FsPromptWorkspaceRegistry,
)
from boba.adapters.in_memory_messages import InMemoryMessageService
from boba.domain.agent.meat.agent import Agent
from boba.domain.agent.models import AgentRequest
from boba.domain.core.tools import ToolContext
from boba.domain.core.workspace import (
    PluginWorkspaceId,
    PromptWorkspaceId,
    WorkspaceId,
)
from boba.domain.llm.models import RequestId
from boba.infra.config import ConfigLoader, SamplingLoader
from boba.infra.container import (
    AgentComponents,
    build_prompt_providers,
    create_agent,
)
from boba.infra.logging import configure_logging
from boba.infra.plugins import PluginContext, PluginLoader
from boba.infra.prompts import PromptLoader

pytestmark = pytest.mark.integration


def _run(query: str, model: str) -> None:
    """Собирает агент с полным стеком middleware и прогоняет один запрос."""
    loader = ConfigLoader()
    app_config = loader.load_app()
    agent_config = loader.load_agent()
    sampling = SamplingLoader().load()
    configure_logging(app_config.log_level, app_config.log_file)

    plugin_workspace = FsPluginWorkspaceRegistry(
        root=Path(app_config.plugins_dir),
    ).get_or_create(PluginWorkspaceId("plugins"))
    tools_service = PluginLoader(
        plugin_workspace,
        PluginContext(
            plugin_workspace=plugin_workspace,
            app_config=app_config,
            agent_config=agent_config,
            sampling=sampling,
        ),
    ).build_tools_service()

    prompt_workspace = FsPromptWorkspaceRegistry(
        root=Path(app_config.prompts_dir),
    ).get_or_create(PromptWorkspaceId("prompts"))
    prompt_providers = build_prompt_providers(
        PromptLoader(prompt_workspace, app_config),
    )

    project_workspace = FsProjectWorkspaceRegistry(
        base_dir=Path(app_config.workspaces.base_dir),
        subdir=app_config.workspaces.user_subdir,
    ).get_or_create(WorkspaceId.new())

    agent: Agent = create_agent(
        llm_config=app_config.llm,
        components=AgentComponents(
            agent_config=agent_config,
            sampling=sampling,
            prompt_providers=prompt_providers,
            message_service=InMemoryMessageService(),
            tools_service=tools_service,
        ),
        tool_ctx=ToolContext(project_workspace=project_workspace),
    )

    request = AgentRequest(
        query=query,
        model=model,
        request_id=RequestId.new(),
    )
    agent.run(agent_config, request)


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
