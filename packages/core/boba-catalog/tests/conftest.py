"""Фикстуры домена каталога: образец и снимок; стенд не нужен."""

from __future__ import annotations

import pytest
from sample_catalog import Sample

from boba.catalog import CatalogSnapshot


@pytest.fixture(scope="session")
def kerberos_workspace() -> None:
    """Домен без I/O: стенд и kerberos тестам не нужны."""


@pytest.fixture
def sample() -> Sample:
    return Sample()


@pytest.fixture
def snapshot(sample: Sample) -> CatalogSnapshot:
    return sample.snapshot()
