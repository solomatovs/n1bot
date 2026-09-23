"""Тип соединения oracle в реестрах: манифест виден через entry points, probe на
живом стенде отдаёт баннер сервера, чужой профиль отвергается."""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from pydantic import BaseModel, ConfigDict

from boba.connections.address import AddressFamilies
from boba.connections.base import ConnectionTypeError
from boba.connections.manifest import ConnectionTypes
from boba.db.oracle.address import OraAddresses
from boba.db.oracle.connection import OracleConfig
from boba.db.oracle.manifest import MANIFEST
from boba.db.postgres.connection import PostgresConfig
from boba.stand.ix import IxStand

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


class OraStandSource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str
    oracle: OracleConfig


class OraStand(IxStand):
    ora_sources: Sequence[OraStandSource]


STAND = OraStand.required()


class TestRegistries:
    def test_connection_type_is_discovered(self) -> None:
        types = ConnectionTypes.discover()

        assert OracleConfig.KIND in types.kinds()
        assert types.manifest_of(OracleConfig.KIND) is MANIFEST
        assert types.kind_of(OracleConfig) == OracleConfig.KIND

    def test_address_family_is_discovered(self) -> None:
        families = AddressFamilies.discover()

        assert families.of_kind("ora_table") is OraAddresses
        assert "oracle" in families.schemes()


class TestProbe:
    @pytest.mark.parametrize("name", [source.name for source in STAND.ora_sources])
    async def test_probe_returns_server_banner(self, name: str) -> None:
        source = next(s for s in STAND.ora_sources if s.name == name)

        banner = await MANIFEST.probe(source.oracle)

        assert banner.startswith("Oracle")

    async def test_foreign_profile_is_refused(self) -> None:
        foreign = PostgresConfig.model_validate(
            {"host": "h", "dbname": "x", "auth": {"method": "trust", "user": "u"}}
        )

        with pytest.raises(ConnectionTypeError, match="expects an OracleConfig"):
            await MANIFEST.probe(foreign)
