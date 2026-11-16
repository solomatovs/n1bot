import base64
import secrets
from collections.abc import AsyncGenerator, Callable

import pytest
from agent_stand import FixtureTokens, InferenceServer, ServiceHost

from boba.agent.acp_server import AgentProfiles
from boba.agent.acp_validator import AcpVersion
from boba.agent.history import HistoryStore
from boba.agent.llm import ProviderConfiguration
from boba.agent.serve import AcpEndpoint, CallerTokens, ServeAgents, ServeRouter
from boba.agent.settings import ServeHistory, ServeSection
from boba.crypto.config import ValueCipherConfig
from boba.db.postgres.connection import PostgresConfig

from .client import V1System


@pytest.fixture
async def system(
    history: HistoryStore,
    inference: InferenceServer,
    postgres: PostgresConfig,
    agent_timeout: float,
) -> AsyncGenerator[V1System]:
    stand = V1System(history, inference, postgres, agent_timeout)
    await stand.hub.start(postgres)
    await stand.seed_provider()
    try:
        yield stand
    finally:
        await stand.close()


@pytest.fixture
async def service(
    postgres: PostgresConfig,
    inference: InferenceServer,
    free_tcp_port_factory: Callable[[], int],
    agent_timeout: float,
) -> AsyncGenerator[ServiceHost]:
    profile = inference.profiles.default(frozenset({"test"})).config
    profiles = {"test": profile}
    encryption = ValueCipherConfig.model_validate(
        {"cipher": base64.b64encode(secrets.token_bytes(32)).decode()}
    )
    section = ServeSection(profiles=profiles, providers=encryption)
    history = ServeHistory(schema="boba", postgres=postgres)
    agents = ServeAgents(section, history, AgentProfiles(profiles), "test", ())
    endpoint = AcpEndpoint(ServeRouter(agents, AcpVersion.V1), CallerTokens(), 30)
    host = ServiceHost(agents, endpoint, FixtureTokens(), free_tcp_port_factory())
    async with host.running(agent_timeout):
        await agents.hub.providers.save(
            ProviderConfiguration(
                owner_id=str(host.tokens.user_id),
                provider_id="test",
                settings=inference.settings,
                headers={},
            ),
            enabled=True,
        )
        yield host
