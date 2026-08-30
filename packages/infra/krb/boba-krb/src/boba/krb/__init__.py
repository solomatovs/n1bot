"""Kerberos для boba: способы аутентификации, SPNEGO-accept, делегирование, PAC."""

from boba.krb.auth import KerberosWorkspace, KerberosWorkspaceConfig
from boba.krb.credentials import (
    CcacheLifetime,
    ClientCredentials,
    DelegatedCredentials,
    IssuedCredentials,
    KerberosCredentials,
    KerberosEnv,
    KeytabCredentials,
    PasswordCredentials,
    TicketCredentials,
)
from boba.krb.delegation import SpnegoAcceptor, SpnegoIdentity, TicketCapture
from boba.krb.pac import PacGroupSids
from boba.krb.spnego import SpnegoNegotiate
from boba.krb.tickets import ServiceTicketIssuer

__all__ = [
    "CcacheLifetime",
    "ClientCredentials",
    "DelegatedCredentials",
    "IssuedCredentials",
    "KerberosCredentials",
    "KerberosEnv",
    "KerberosWorkspace",
    "KerberosWorkspaceConfig",
    "KeytabCredentials",
    "PacGroupSids",
    "PasswordCredentials",
    "ServiceTicketIssuer",
    "SpnegoAcceptor",
    "SpnegoIdentity",
    "SpnegoNegotiate",
    "TicketCapture",
    "TicketCredentials",
]
