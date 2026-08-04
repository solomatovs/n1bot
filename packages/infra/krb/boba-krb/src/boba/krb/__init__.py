"""Kerberos для boba: креды из keytab, SPNEGO-accept, S4U-делегирование, PAC-SID."""

from boba.krb.config import AcceptConfig, DelegationConfig, KeytabConfig
from boba.krb.credentials import (
    CcacheRegistry,
    DelegatedCredentials,
    KerberosCredentials,
    KerberosEnv,
    KeytabCredentials,
    UserCcache,
)
from boba.krb.delegation import KerberosDelegation, SpnegoAcceptor, SpnegoIdentity
from boba.krb.errors import (
    CredentialsExpiredError,
    DelegationNotPermittedError,
    InvalidTokenError,
    KerberosError,
    KeytabError,
)
from boba.krb.pac import PacGroupSids

__all__ = [
    "AcceptConfig",
    "CcacheRegistry",
    "CredentialsExpiredError",
    "DelegatedCredentials",
    "DelegationConfig",
    "DelegationNotPermittedError",
    "InvalidTokenError",
    "KerberosCredentials",
    "KerberosDelegation",
    "KerberosEnv",
    "KerberosError",
    "KeytabConfig",
    "KeytabCredentials",
    "KeytabError",
    "PacGroupSids",
    "SpnegoAcceptor",
    "SpnegoIdentity",
    "UserCcache",
]
