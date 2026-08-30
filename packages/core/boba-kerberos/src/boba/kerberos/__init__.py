"""Kerberos без gssapi: секции профилей, вход и делегирование, ошибки слоя."""

from boba.kerberos.errors import (
    CredentialsExpiredError,
    DelegationNotPermittedError,
    InvalidTokenError,
    KerberosError,
    KeytabError,
    TicketSealError,
)
from boba.kerberos.sections import (
    CcacheKind,
    DelegatedAuth,
    KerberosAuth,
    KerberosAuthBase,
    KerberosDump,
    KerberosMethod,
    KerberosPasswordAuth,
    KeytabAuth,
    TicketAuth,
)
from boba.kerberos.signin import (
    AcceptConfig,
    ConstrainedDelegation,
    Delegation,
    DelegationMode,
    ForwardedDelegation,
    NegotiateToken,
    SignInCredentials,
    SignInTicket,
)

__all__ = [
    "AcceptConfig",
    "CcacheKind",
    "ConstrainedDelegation",
    "CredentialsExpiredError",
    "DelegatedAuth",
    "Delegation",
    "DelegationMode",
    "DelegationNotPermittedError",
    "ForwardedDelegation",
    "InvalidTokenError",
    "KerberosAuth",
    "KerberosAuthBase",
    "KerberosDump",
    "KerberosError",
    "KerberosMethod",
    "KerberosPasswordAuth",
    "KeytabAuth",
    "KeytabError",
    "NegotiateToken",
    "SignInCredentials",
    "SignInTicket",
    "TicketAuth",
    "TicketSealError",
]
