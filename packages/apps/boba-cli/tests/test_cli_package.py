"""Пакет собран и импортируется: пустая проверка этапа 0."""

from __future__ import annotations

import importlib


class TestPackage:
    def test_imports(self) -> None:
        module = importlib.import_module("boba.cli")

        assert module.__doc__
