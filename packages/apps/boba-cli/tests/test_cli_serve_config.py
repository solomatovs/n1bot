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

from boba.cli.serve import ServeConfig, ServeSection
from boba.config import ConfigLoader, bind

INSTANCE = """
[env]
    listen_host = "0.0.0.0"
    port        = 8660
    public_url  = "https://boba.example.com/boba"
    url_prefix  = "/boba"
    data        = "${env.base}/data"
    base        = "/srv/boba"

[krb]
    config     = "${env.base}/conf/krb/krb5.conf"
    ccache_dir = "${env.data}/krb/cache"

[app]
    schema  = "agent"
    krb     = "${krb}"
    session = "${session}"
    agent   = "${agent}"
    auth    = "${oc.dict.values:auth}"

[keys]
    session      = "site-auth-secret-of-the-stand"
    agent_proxy  = "site-agent-proxy-secret-of-the-stand"
    agent_client = "agent-client-secret-of-the-stand"

[ldap]
    server  = "ldaps://dc.example.com:3269"
    base_dn = "DC=example,DC=com"
    [ldap.auth]
        method   = "simple"
        user     = "reader@example.com"
        password = "reader-password"
    [ldap.mapping]
        dn = {}
        [ldap.mapping.member_of]
            "CN=boba,DC=example,DC=com" = ["read"]

[postgres_history]
    host             = "pg.example.com"
    hostaddr         = "10.0.0.5"
    port             = 5432
    dbname           = "aid"
    sslmode          = "disable"
    application_name = "boba-agent"
    connect_timeout  = 10
    [postgres_history.auth]
        method   = "password"
        user     = "boba_history"
        password = "history-password"
    [postgres_history.pool]
        min_size = 2
        max_size = 16
        timeout  = 30

[llm]
    [llm.main]
        kind            = "openai"
        baseUrl         = "https://llm.example.com/v1"
        sslVerify       = true
        timeoutSec      = 600
        retryAttempts   = 3
        retryBackoffSec = 1.0
        [llm.main.auth]
            method = "bearer"
            token  = "llm-token-of-the-stand"
        [llm.main.retryStatuses]
            429 = 5

[model_limits]
    [model_limits.main]
        reasoningReturn = "turn"

[agent]
    host                 = "${env.listen_host}"
    port                 = "${env.port}"
    path                 = "/acp"
    public_url           = "${env.public_url}"
    url_prefix           = "${env.url_prefix}"
    shutdown_timeout_sec = 10
    idle_sec             = 1800
    [agent.history]
        schema   = "boba"
        postgres = "${postgres_history}"
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
                provider        = "${llm.main}"
                model           = "general-model"
                systemPrompt    = "general prompt of the stand"
                contextWindow   = 128000
                maxOutputTokens = 8192
                reasoningReturn = "${model_limits.main.reasoningReturn}"
                [agent.profiles.general.model.sampling]
                    temperature      = 1.0
                    top_p            = 0.95
                    presence_penalty = 0.0
                    max_tokens       = 16000
                    reasoning_effort = "low"
        [agent.profiles.search]
            roles = ["*"]
            [agent.profiles.search.model]
                provider        = "${llm.main}"
                model           = "search-model"
                systemPrompt    = "search prompt of the stand"
                contextWindow   = 128000
                maxOutputTokens = 8192
                reasoningReturn = "${model_limits.main.reasoningReturn}"
    [agent.clients]
        [agent.clients.boba-chat]
            secret = "${keys.agent_client}"

[auth]
    [auth.ldap]
        type             = "ldap"
        server           = "${ldap.server}"
        base_dn          = "${ldap.base_dn}"
        user_filter      = "(sAMAccountName={username})"
        bind_dn_template = "EXAMPLE\\\\{username}"
        [auth.ldap.roles]
            directory = "${ldap.mapping}"
    [auth.local]
        type          = "local"
        require_roles = true
        [auth.local.users]
            admin = "admin-password"
        [auth.local.roles]
            [auth.local.roles.local]
                [auth.local.roles.local.mapping]
                    admin = ["read"]
    [auth.proxy]
        type          = "proxy"
        secret        = "${keys.agent_proxy}"
        max_skew_sec  = 60
        require_roles = true
        [auth.proxy.roles]
            [auth.proxy.roles.header]
                name = "X-Remote-Roles"

[session]
    auth_secret     = "${keys.session}"
    session_ttl_sec = 3600
    session_max_sec = 28800
    generation      = ""
"""


@pytest.fixture
def raw(tmp_path: Path) -> DictConfig:
    config = tmp_path / "config.toml"
    config.write_text(INSTANCE, encoding="utf-8")

    return ConfigLoader([config]).load()


class TestConfig:
    PROFILES: ClassVar[list[str]] = ["general", "search"]

    def test_the_instance_config_binds(self, raw: DictConfig) -> None:
        config = bind(raw, path=ServeConfig.SECTION, model=ServeConfig)

        agent = config.agent
        assert list(agent.profiles) == self.PROFILES
        assert agent.path == "/acp"
        assert agent.url_prefix == "/boba"
        assert (
            agent.profiles["general"].model.system_prompt
            == "general prompt of the stand"
        )
        history = agent.history
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
        section = OmegaConf.to_container(OmegaConf.select(raw, "agent"), resolve=True)
        assert isinstance(section, dict)
        section["url_prefix"] = "/other"

        with pytest.raises(ValidationError, match="expected the url_prefix '/other'"):
            ServeSection.model_validate(section)

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
