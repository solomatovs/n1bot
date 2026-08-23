"""Kerberos для boba: креды из keytab, SPNEGO-accept, S4U-делегирование, PAC-SID."""

from boba.krb.config import (
    AcceptConfig,
    ClientKerberos,
    ConstrainedDelegation,
    DelegatedConfig,
    Delegation,
    DelegationMode,
    ForwardedDelegation,
    Kerberos,
    KerberosDump,
    KerberosKind,
    KeytabConfig,
    TicketConfig,
)
from boba.krb.credentials import (
    CcacheLifetime,
    CcacheRegistry,
    ClientCredentials,
    DelegatedCredentials,
    KerberosCredentials,
    KerberosEnv,
    KeytabCredentials,
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
from boba.krb.spnego import SpnegoNegotiate
from boba.krb.tickets import ServiceTicketIssuer

__all__ = [
    "AcceptConfig",
    "CcacheLifetime",
    "CcacheRegistry",
    "ClientCredentials",
    "ClientKerberos",
    "ConstrainedDelegation",
    "CredentialsExpiredError",
    "DelegatedConfig",
    "DelegatedCredentials",
    "Delegation",
    "DelegationMode",
    "DelegationNotPermittedError",
    "ForwardedDelegation",
    "InvalidTokenError",
    "Kerberos",
    "KerberosCredentials",
    "KerberosDelegation",
    "KerberosDump",
    "KerberosEnv",
    "KerberosError",
    "KerberosKind",
    "KeytabConfig",
    "KeytabCredentials",
    "KeytabError",
    "PacGroupSids",
    "ServiceTicketIssuer",
    "SpnegoAcceptor",
    "SpnegoIdentity",
    "SpnegoNegotiate",
    "TicketConfig",
    "TicketCredentials",
    "UserCcache",
]
