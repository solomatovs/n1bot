"""Конфиг экземпляра службы агента `boba serve` разбирается её моделями:
провайдер модели и пределы — одними объектами для всех профилей,
история — своим подключением с пулом; слой настроек агента с историей,
профилями моделей или профилем по умолчанию — отказ на разборе."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest
from omegaconf import DictConfig, OmegaConf
from pydantic import ValidationError

from boba.cli.serve import AgentServerConfig, ServeConfig, ServeSection
from boba.config import bind
from boba.config.application import ConfigFiles

INSTANCE = r"""
[paths]
data_dir = "/srv/boba/data"

[env]
listen_host = "0.0.0.0"
port = 8660
public_url = "https://boba.example.com/boba"
url_prefix = "/boba"

[app]
schema = "boba"

[keys]
session = "site-auth-secret-of-the-stand"
agent_proxy = "site-agent-proxy-secret-of-the-stand"
agent_client = "agent-client-secret-of-the-stand"

[llm]

[llm.main]
kind = "openai"
baseUrl = "https://llm.example.com/v1"
sslVerify = true
timeoutSec = 600
retryAttempts = 3
retryBackoffSec = 1.0

[llm.main.auth]
method = "bearer"
token = "llm-token-of-the-stand"

[llm.main.retryStatuses]
429 = 5

[model_limits]

[model_limits.main]
reasoningReturn = "turn"

[agent]
path = "/acp"
idle_sec = 1800

[agent.settings]

[agent.settings.permissions]
defaultMode = "bypassPermissions"
allowBypass = true

[agent.settings.mcp]
callTimeoutSec = 86400

[agent.settings.tools]
alwaysLoad = []

[agent.settings.sessions]
maxOpen = 256

[agent.settings.limits]
toolResultWindowShare = 0.25

[agent.profiles]

[agent.profiles.general]
roles = ["*"]

[agent.profiles.general.model]
provider = "${llm.main}"
model = "general-model"
systemPrompt = "general prompt of the stand"
contextWindow = 128000
maxOutputTokens = 8192
reasoningReturn = "${model_limits.main.reasoningReturn}"

[agent.profiles.general.model.sampling]
temperature = 1.0
top_p = 0.95
presence_penalty = 0.0
max_tokens = 16000
reasoning_effort = "low"

[agent.profiles.search]
roles = ["*"]

[agent.profiles.search.model]
provider = "${llm.main}"
model = "search-model"
systemPrompt = "search prompt of the stand"
contextWindow = 128000
maxOutputTokens = 8192
reasoningReturn = "${model_limits.main.reasoningReturn}"

[agent.clients]

[agent.clients.boba-chat]
secret = "${keys.agent_client}"

[[auth]]
type = "ldap"
server = "ldaps://dc.example.com:3269"
base_dn = "DC=example,DC=com"
user_filter = "(sAMAccountName={username})"
bind_dn_template = "EXAMPLE\\{username}"

[auth.identity]
realm = "corporate"

[auth.roles.directory]

[auth.roles.directory.member_of]
"CN=boba,DC=example,DC=com" = ["read"]

[[auth]]
type = "local"
require_roles = true

[auth.identity]
realm = "boba-local"

[auth.users]
admin = "admin-password"

[auth.roles.local.mapping]
admin = ["read"]

[[auth]]
type = "proxy"
secret = "${keys.agent_proxy}"
max_skew_sec = 60
require_roles = true

[auth.identity]
realm = "corporate"

[auth.roles.header]
name = "X-Remote-Roles"

[auth.profiles.header]
name = "X-Remote-Profiles"
selected = "X-Remote-Profile"

[session]
auth_secret = "${keys.session}"
session_ttl_sec = 3600
session_max_sec = 28800
generation = ""

[tls]
ca_file = "/etc/ssl/certs/ca-certificates.crt"

[server]
host = "${env.listen_host}"
port = "${env.port}"
public_url = "${env.public_url}"
url_prefix = "${env.url_prefix}"
shutdown_timeout_sec = 10

[postgres]
host = "pg.example.com"
hostaddr = "10.0.0.5"
port = 5432
dbname = "aid"
sslmode = "disable"
application_name = "boba-agent"
connect_timeout = 10

[postgres.auth]
method = "password"
user = "boba_history"
password = "history-password"

[postgres.pool]
min_size = 2
max_size = 16
timeout = 30

"""


@pytest.fixture
def raw(tmp_path: Path) -> DictConfig:
    config = tmp_path / "config.toml"
    config.write_text(INSTANCE, encoding="utf-8")

    return ConfigFiles(ServeConfig).read([config])


class TestConfig:
    PROFILES: ClassVar[list[str]] = ["general", "search"]

    def test_service_needs_no_technical_overrides(self, raw: DictConfig) -> None:
        section = OmegaConf.to_container(raw.agent, resolve=True)
        assert isinstance(section, dict)
        for key in (
            "path",
            "idle_sec",
            "settings",
            "clients",
        ):
            del section[key]

        config = ServeSection.model_validate(section)

        assert config.path == "/acp"
        assert config.idle_sec == 1800
        assert config.clients == {}
        assert config.settings.model is None
        assert list(config.profiles) == self.PROFILES

    def test_the_instance_config_binds(self, raw: DictConfig) -> None:
        config = bind(raw, path="app", model=ServeConfig)

        agent = config.agent
        assert list(agent.profiles) == self.PROFILES
        assert agent.path == "/acp"
        assert config.server.url_prefix == "/boba"
        assert (
            agent.profiles["general"].model.system_prompt
            == "general prompt of the stand"
        )
        history = config.history
        assert history.db_schema == "boba"
        assert history.postgres.host == "pg.example.com"
        assert history.postgres.application_name == "boba-agent"
        assert (history.postgres.pool.min_size, history.postgres.pool.max_size) == (
            2,
            16,
        )
        general = agent.profiles["general"].model
        search = agent.profiles["search"].model
        assert general.provider == search.provider
        assert general.provider.base_url == "https://llm.example.com/v1"
        assert general.context_window == search.context_window == 128000
        assert general.model == "general-model"
        assert agent.settings.sessions is not None
        assert agent.settings.sessions.max_open == 256
        assert list(agent.clients) == ["boba-chat"]

    def test_public_url_outside_the_prefix_is_refused(self, raw: DictConfig) -> None:
        section = OmegaConf.to_container(OmegaConf.select(raw, "server"), resolve=True)
        assert isinstance(section, dict)
        section["url_prefix"] = "/other"

        with pytest.raises(ValidationError, match="expected the url_prefix '/other'"):
            AgentServerConfig.model_validate(section)

    @pytest.mark.parametrize(
        ("key", "value", "refusal"),
        [
            ("history", {"kind": "memory"}, "the history is \\[agent.history\\]"),
            ("models", {"x": {}}, "model profiles live in"),
            ("model", "general", "the default model is the first profile"),
        ],
    )
    def test_settings_layer_refusals(
        self, raw: DictConfig, key: str, value: Any, refusal: str
    ) -> None:
        section = OmegaConf.to_container(OmegaConf.select(raw, "agent"), resolve=True)
        assert isinstance(section, dict)
        section["settings"][key] = value

        with pytest.raises(ValidationError, match=refusal):
            ServeSection.model_validate(section)
