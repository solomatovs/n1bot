"""Допуск SSO: без разобранного PAC при настроенных исключениях по SID входа нет."""

from __future__ import annotations

from typing import Any

import pytest
from omegaconf import DictConfig

from boba.auth.config import (
    KerberosAuthConfig,
    KerberosRoleProviders,
    PrincipalRolesConfig,
)
from boba.config import bind
from boba.identity.admission import RoleExcludeConfig, RoleMappingConfig
from boba.identity.errors import AuthorizationError
from boba.krb import SpnegoIdentity
from boba.stand.signin import SignInStand

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

USERNAME = "reader"


def _config(
    raw_config: DictConfig, principal: PrincipalRolesConfig
) -> KerberosAuthConfig:
    """Секция стенда с единственным провайдером principal: без каталога."""
    config = bind(raw_config, path="auth.kerberos", model=KerberosAuthConfig)
    providers = KerberosRoleProviders(principal=principal)

    return config.model_copy(update={"roles": providers, "require_roles": False})


def _principal(config: KerberosAuthConfig) -> str:
    """Принципал стенда по его же principal_format: вход разбирает логин из него."""
    return config.principal_format.replace("{username}", USERNAME)


async def test_unparsed_pac_is_refused_when_sid_exclusions_are_configured(
    raw_config: Any,
) -> None:
    roles = PrincipalRolesConfig(sid_ex=RoleExcludeConfig(root=["S-1-5-21-1"]))
    config = _config(raw_config, roles)
    sign_in = SignInStand.assembly().sso(config, "secret")

    identity = SpnegoIdentity(principal=_principal(config), pac_parsed=False)

    with pytest.raises(AuthorizationError):
        await sign_in.signed_in(identity, "")


async def test_unparsed_pac_passes_without_sid_exclusions(raw_config: Any) -> None:
    config = _config(raw_config, PrincipalRolesConfig())
    principal = _principal(config)
    roles = PrincipalRolesConfig(principal=RoleMappingConfig(root={principal: ["DEV"]}))
    sign_in = SignInStand.assembly().sso(_config(raw_config, roles), "secret")

    identity = SpnegoIdentity(principal=principal, pac_parsed=False)

    signed = await sign_in.signed_in(identity, "")

    assert "DEV" in signed.sign_in.roles
