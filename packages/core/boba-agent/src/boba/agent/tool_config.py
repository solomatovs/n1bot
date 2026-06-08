"""Composition-слой: порты boba.tools ↔ OmegaConf-резолверы boba.settings.

boba-tools определяет порты ConfigResolver/PluginToolFilter и намеренно не
знает про источник конфигурации. Конкретные реализации поверх OmegaConf живут в
boba.settings; здесь — единая точка импорта для composition-слоя агента
(резолвер/фильтр строятся вокруг явного конфиг-инстанса из build_app_config).
"""

from __future__ import annotations

from boba.settings import (
    OmegaConfPluginToolFilter,
    OmegaConfResolver,
    PluginConfigBase,
    bind,
    build_app_config,
)

__all__ = [
    "OmegaConfPluginToolFilter",
    "OmegaConfResolver",
    "PluginConfigBase",
    "bind",
    "build_app_config",
]
