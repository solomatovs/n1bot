"""Тип соединения oracle в реестрах: манифест виден через entry points, чужой
профиль адресом отвергается."""

from __future__ import annotations

import pytest

from boba.connections.address import AddressFamilies
from boba.connections.base import ConnectionTypeError
from boba.connections.manifest import ConnectionTypes
from boba.db.oracle.address import OraAddresses
from boba.db.oracle.connection import OracleConfig
from boba.db.oracle.manifest import MANIFEST
from boba.db.postgres.connection import PostgresConfig

pytestmark = pytest.mark.integration


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

    def test_foreign_profile_is_refused(self) -> None:
        foreign = PostgresConfig.model_validate(
            {"host": "h", "dbname": "x", "auth": {"method": "trust", "user": "u"}}
        )

        with pytest.raises(ConnectionTypeError, match="expects an OracleConfig"):
            MANIFEST.address(foreign)
