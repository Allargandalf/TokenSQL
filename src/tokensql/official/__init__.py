"""Independent official-price collection service."""

from .base import FetchResult, OfficialRecord, OfficialSource
from .configured import (
    ConfiguredAdapterError,
    ConfiguredOfficialAdapterConfig,
    ConfiguredOfficialJsonAdapter,
    create_adapter,
    load_config,
)
from .http import OfficialHttpClient

__all__ = [
    "ConfiguredAdapterError",
    "ConfiguredOfficialAdapterConfig",
    "ConfiguredOfficialJsonAdapter",
    "FetchResult",
    "OfficialHttpClient",
    "OfficialRecord",
    "OfficialSource",
    "create_adapter",
    "load_config",
]
