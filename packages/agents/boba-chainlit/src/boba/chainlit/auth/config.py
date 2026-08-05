from typing import Annotated

from pydantic import Field

from boba.chainlit.auth.kerberos import KerberosAuthConfig
from boba.chainlit.auth.ldap import LdapAuthConfig
from boba.chainlit.auth.local import LocalAuthConfig

__all__ = ["AuthConfig"]


AuthConfig = Annotated[
    LocalAuthConfig | KerberosAuthConfig | LdapAuthConfig,
    Field(discriminator="type"),
]
