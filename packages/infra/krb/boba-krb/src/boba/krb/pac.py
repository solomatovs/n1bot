"""SID-ы групп пользователя из PAC kerberos-тикета (MS-PAC KERB_VALIDATION_INFO).

Ошибки: ValueError — PAC присутствует, но его logon-info не разбирается.
"""

from __future__ import annotations

from typing import ClassVar

from gssapi import SecurityContext
from gssapi.raw import get_name_attribute
from gssapi.raw.misc import GSSError

__all__ = ["PacGroupSids"]


class PacGroupSids:
    """Извлечение logon-info из контекста"""

    ATTR_LOGON_INFO: ClassVar[bytes] = b"urn:mspac:logon-info"
    NDR_VERSION: ClassVar[int] = 1
    NDR_LITTLE_ENDIAN: ClassVar[int] = 0x10

    class _Ndr:
        "NDR-парсер KERB_VALIDATION_INFO"

        __slots__ = ("buf", "pos")

        def __init__(self, buf: bytes) -> None:
            self.buf = buf
            self.pos = 0

        def take(self, n: int) -> bytes:
            if self.pos + n > len(self.buf):
                raise ValueError("truncated PAC logon-info buffer")
            out = self.buf[self.pos : self.pos + n]
            self.pos += n
            return out

        def skip(self, n: int) -> None:
            self.take(n)

        def u8(self) -> int:
            return self.take(1)[0]

        def u32(self) -> int:
            return int.from_bytes(self.take(4), "little")

        def align4(self) -> None:
            self.skip((-self.pos) % 4)

        def skip_unistr(self) -> None:
            "Пропускает deferred-буфер RPC_UNICODE_STRING (conformant varying)."
            self.align4()
            self.skip(8)  # MaxCount, Offset
            actual = self.u32()
            self.skip(actual * 2)

        def read_rid_array(self, count: int) -> list[int]:
            "Deferred GROUP_MEMBERSHIP[] (conformant): RID-ы без Attributes."
            self.align4()
            self.skip(4)  # MaxCount
            rids = []
            for _ in range(count):
                rids.append(self.u32())
                self.skip(4)  # Attributes
            return rids

        def read_sid(self) -> str:
            "Deferred PISID (conformant: MaxCount + RPC_SID) -> строка S-1-...."
            self.align4()
            self.skip(4)  # MaxCount = SubAuthorityCount
            revision = self.u8()
            sub_count = self.u8()
            authority = int.from_bytes(self.take(6), "big")
            subs = [self.u32() for _ in range(sub_count)]
            return "S-" + "-".join(str(x) for x in (revision, authority, *subs))

    @staticmethod
    def of_context(ctx: SecurityContext) -> list[str]:
        """SID-ы групп инициатора; [] если PAC недоступен."""
        try:
            attr = get_name_attribute(ctx.initiator_name, PacGroupSids.ATTR_LOGON_INFO)
        except GSSError:
            # PAC в тикете нет или механизм его не отдаёт — не ошибка
            return []

        # PAC подписан KDC; берём только проверенные значения
        if not attr.authenticated or not attr.values:
            return []

        return PacGroupSids.parse_logon_info(attr.values[0])

    @staticmethod
    def parse_logon_info(blob: bytes) -> list[str]:  # noqa: C901, PLR0912
        """KERB_VALIDATION_INFO (NDR) -> SID-ы групп пользователя."""
        r = PacGroupSids._Ndr(blob)

        # common type header (MS-RPCE type serialization v1): версия, LE
        if (
            r.u8() != PacGroupSids.NDR_VERSION
            or r.u8() != PacGroupSids.NDR_LITTLE_ENDIAN
        ):
            raise ValueError("unexpected PAC logon-info NDR header")
        r.skip(6)  # остаток common header
        r.skip(8)  # private header (ObjectBufferLength, Filler)

        if r.u32() == 0:  # top-level указатель на KERB_VALIDATION_INFO
            return []

        r.skip(48)  # 6 x FILETIME (LogonTime..PasswordMustChange)
        name_ptrs = []
        for _ in range(6):  # EffectiveName..HomeDirectoryDrive
            r.skip(4)  # Length, MaximumLength
            name_ptrs.append(r.u32())
        r.skip(4)  # LogonCount, BadPasswordCount
        r.skip(8)  # UserId, PrimaryGroupId
        group_count = r.u32()
        group_ids_ptr = r.u32()
        r.skip(4)  # UserFlags
        r.skip(16)  # UserSessionKey
        server_ptrs = []
        for _ in range(2):  # LogonServer, LogonDomainName
            r.skip(4)
            server_ptrs.append(r.u32())
        domain_ptr = r.u32()  # LogonDomainId
        r.skip(8)  # Reserved1[2]
        r.skip(8)  # UserAccountControl, SubAuthStatus
        r.skip(16)  # LastSuccessfulILogon, LastFailedILogon
        r.skip(8)  # FailedILogonCount, Reserved3
        sid_count = r.u32()
        extra_sids_ptr = r.u32()
        rg_domain_ptr = r.u32()  # ResourceGroupDomainSid
        rg_count = r.u32()
        rg_ids_ptr = r.u32()

        for p in name_ptrs:
            if p:
                r.skip_unistr()

        rids = r.read_rid_array(group_count) if group_ids_ptr else []

        for p in server_ptrs:
            if p:
                r.skip_unistr()

        sids: list[str] = []
        if domain_ptr:
            domain = r.read_sid()
            sids.extend(f"{domain}-{rid}" for rid in rids)

        if extra_sids_ptr:
            r.align4()
            r.skip(4)  # MaxCount
            extra_ptrs = []
            for _ in range(sid_count):
                extra_ptrs.append(r.u32())
                r.skip(4)  # Attributes
            sids.extend(r.read_sid() for p in extra_ptrs if p)

        if rg_domain_ptr:
            rg_domain = r.read_sid()
            if rg_ids_ptr:
                sids.extend(f"{rg_domain}-{rid}" for rid in r.read_rid_array(rg_count))

        return sids
