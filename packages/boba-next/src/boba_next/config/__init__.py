"""Конфиг-runtime + application-слой (секции и реестр DTO)."""

from boba_next.config.app import (
    CONFIG_SECTIONS_ENTRY_POINT,
    AppConfig,
    AppConfigFactory,
    ConfigError,
    SectionMissingError,
)
from boba_next.config.bootstrap import AppConfigBootstrap
from boba_next.config.bundle import (
    ConfigBundle,
    ConfigBundleFactory,
    FlatConfigMaterializer,
)
from boba_next.config.flat import FlatConfig
from boba_next.config.path import (
    ConfigLookup,
    ConfigPath,
    ConfigPathParseError,
    ConfigSource,
    ConfigSpace,
    Found,
    IndexSegment,
    NameSegment,
    NotFound,
    Segment,
)
from boba_next.config.section import ConfigSection

__all__ = [
    "CONFIG_SECTIONS_ENTRY_POINT",
    "AppConfig",
    "AppConfigBootstrap",
    "AppConfigFactory",
    "ConfigBundle",
    "ConfigBundleFactory",
    "ConfigError",
    "ConfigLookup",
    "ConfigPath",
    "ConfigPathParseError",
    "ConfigSection",
    "ConfigSource",
    "ConfigSpace",
    "FlatConfig",
    "FlatConfigMaterializer",
    "Found",
    "IndexSegment",
    "NameSegment",
    "NotFound",
    "SectionMissingError",
    "Segment",
]
