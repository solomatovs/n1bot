from typing import Annotated

from pydantic import Field

from boba.auth.kerberos import KerberosAuthConfig
from boba.auth.ldap import LdapAuthConfig
from boba.auth.local import LocalAuthConfig

__all__ = ["AuthConfig"]


AuthConfig = Annotated[
    LocalAuthConfig | KerberosAuthConfig | LdapAuthConfig,
    Field(discriminator="type"),
]
