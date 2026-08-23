"""Kerberos для boba: способы аутентификации, SPNEGO-accept, делегирование, PAC."""

from boba.krb.auth import (
    DelegatedAuth,
    KerberosAuth,
    KerberosAuthBase,
    KerberosMethod,
    KerberosPasswordAuth,
    KerberosWorkspace,
    KerberosWorkspaceConfig,
    KeytabAuth,
    TicketAuth,
)
from boba.krb.config import (
    AcceptConfig,
    ConstrainedDelegation,
    Delegation,
    DelegationMode,
    ForwardedDelegation,
    KerberosDump,
)
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
    "AcceptConfig",
    "CcacheLifetime",
    "CcacheRegistry",
    "ClientCredentials",
    "ConstrainedDelegation",
    "CredentialsExpiredError",
    "DelegatedAuth",
    "DelegatedCredentials",
    "Delegation",
    "DelegationMode",
    "DelegationNotPermittedError",
    "ForwardedDelegation",
    "InvalidTokenError",
    "IssuedCredentials",
    "KerberosAuth",
    "KerberosAuthBase",
    "KerberosCredentials",
    "KerberosDelegation",
    "KerberosDump",
    "KerberosEnv",
    "KerberosError",
    "KerberosMethod",
    "KerberosPasswordAuth",
    "KerberosWorkspace",
    "KerberosWorkspaceConfig",
    "KeytabAuth",
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
    "TicketAuth",
    "TicketCredentials",
    "UserCcache",
]
