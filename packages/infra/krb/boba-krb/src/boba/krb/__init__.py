"""Kerberos для boba: креды из keytab, SPNEGO-accept, S4U-делегирование, PAC-SID."""

from boba.krb.config import (
    AcceptConfig,
    CcacheConfig,
    ClientKerberos,
    DelegationConfig,
    Kerberos,
    KeytabConfig,
)
from boba.krb.credentials import (
    CcacheCredentials,
    CcacheRegistry,
    ClientCredentials,
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
    "CcacheConfig",
    "CcacheCredentials",
    "CcacheRegistry",
    "ClientCredentials",
    "ClientKerberos",
    "CredentialsExpiredError",
    "DelegatedCredentials",
    "DelegationConfig",
    "DelegationNotPermittedError",
    "InvalidTokenError",
    "Kerberos",
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
