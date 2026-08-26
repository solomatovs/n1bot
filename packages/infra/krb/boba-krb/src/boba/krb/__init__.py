"""Kerberos для boba: способы аутентификации, SPNEGO-accept, делегирование, PAC."""

from boba.krb.auth import KerberosWorkspace, KerberosWorkspaceConfig
from boba.krb.credentials import (
    CcacheLifetime,
    CcacheRegistry,
    ClientCredentials,
    DelegatedCredentials,
    IssuedCredentials,
    KerberosCredentials,
    KerberosEnv,
    KeytabCredentials,
    PasswordCredentials,
    TicketCredentials,
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
from boba.krb.refresh import RefreshWaiters, RefreshWaiting
from boba.krb.spnego import SpnegoNegotiate
from boba.krb.tickets import ServiceTicketIssuer

__all__ = [
    "CcacheLifetime",
    "CcacheRegistry",
    "ClientCredentials",
    "CredentialsExpiredError",
    "DelegatedCredentials",
    "DelegationNotPermittedError",
    "InvalidTokenError",
    "IssuedCredentials",
    "KerberosCredentials",
    "KerberosDelegation",
    "KerberosEnv",
    "KerberosError",
    "KerberosWorkspace",
    "KerberosWorkspaceConfig",
    "KeytabCredentials",
    "KeytabError",
    "PacGroupSids",
    "PasswordCredentials",
    "RefreshWaiters",
    "RefreshWaiting",
    "ServiceTicketIssuer",
    "SpnegoAcceptor",
    "SpnegoIdentity",
    "SpnegoNegotiate",
    "TicketCredentials",
    "UserCcache",
]
