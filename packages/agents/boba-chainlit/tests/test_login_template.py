"""Шаблоны входа с {username}: подстановка, логин из принципала, проверка конфига."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from boba.chainlit.auth.config import LdapAuthConfig
from boba.identity.session import LoginTemplate
from boba.toolkit.template import TemplateError

pytestmark = pytest.mark.anyio


class TestLoginTemplate:
    async def test_username_of_principal_formats(self) -> None:
        assert (
            LoginTemplate.username_of("{username}@CORP.LOCAL", "bob@CORP.LOCAL")
            == "bob"
        )
        assert LoginTemplate.username_of("CORP\\{username}", "CORP\\bob") == "bob"

    async def test_foreign_principal_is_rejected(self) -> None:
        with pytest.raises(TemplateError):
            LoginTemplate.username_of("{username}@CORP.LOCAL", "bob@OTHER.LOCAL")

    async def test_render_ldap_templates(self) -> None:
        assert LoginTemplate.render("(sAMAccountName={username})", "bob") == (
            "(sAMAccountName=bob)"
        )
        assert LoginTemplate.render("{username}@corp.local", "bob") == "bob@corp.local"

    async def test_check_requires_username_field(self) -> None:
        assert LoginTemplate.check("{username}@X") == "{username}@X"

        with pytest.raises(ValueError, match="username"):
            LoginTemplate.check("user@X")

        with pytest.raises(ValueError, match="bad template"):
            LoginTemplate.check("{username")

    async def test_ldap_config_validates_templates(self) -> None:
        with pytest.raises(ValidationError, match="username"):
            LdapAuthConfig.model_validate(
                {
                    "server": "ldaps://dc",
                    "base_dn": "DC=x",
                    "bind_dn_template": "no-field@x",
                }
            )
