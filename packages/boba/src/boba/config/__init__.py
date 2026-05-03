"""Конфиг-runtime + application-слой (секции и реестр DTO)."""

from boba.config.app import (
    CONFIG_SECTIONS_ENTRY_POINT,
    AppConfig,
    AppConfigFactory,
    ConfigError,
    SectionMissingError,
)
from boba.config.bootstrap import AppConfigBootstrap
from boba.config.bundle import (
    ConfigBundle,
    ConfigBundleFactory,
    FlatConfigMaterializer,
)
from boba.config.flat import FlatConfig
from boba.config.path import (
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
from boba.config.section import ConfigSection

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
