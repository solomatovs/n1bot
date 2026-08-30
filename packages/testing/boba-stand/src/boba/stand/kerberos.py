"""Клиентская сторона kerberos-входа в тестах: TGT по паролю и AP-REQ к SPN сервиса."""

from pathlib import Path

import krb5
from gssapi import Credentials, Name, NameType, SecurityContext

from boba.stand.site import Stand


class SsoBrowser:
    """Браузер пользователя стенда: билет для заголовка Negotiate."""

    @staticmethod
    def token(stand: Stand, tmp_path: Path) -> bytes:
        password = stand.reader_password.get_secret_value()
        principal = stand.reader_principal
        spn = f"HTTP/{stand.krb_domain}@{stand.krb_realm}"

        context = krb5.init_context()
        user = krb5.parse_name_flags(context, principal.encode())
        options = krb5.get_init_creds_opt_alloc(context)
        krb5.get_init_creds_opt_set_forwardable(options, True)
        tgt = krb5.get_init_creds_password(context, user, options, password.encode())
        ccache = f"FILE:{tmp_path / 'browser'}"
        cache = krb5.cc_resolve(context, ccache.encode())
        krb5.cc_initialize(context, cache, user)
        krb5.cc_store_cred(context, cache, tgt)

        creds = Credentials(usage="initiate", store={b"ccache": ccache.encode()})
        target = Name(spn, NameType.kerberos_principal)
        initiator = SecurityContext(name=target, creds=creds, usage="initiate", flags=0)
        return initiator.step()
