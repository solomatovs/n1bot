"""Chainlit-интеграция: sink-мост к UI, сессия, загрузки и конфиг.

Короткие импорты::

    from boba_chainlit import ChainlitBridgeSink, ChatSession
    from boba_chainlit import ChainlitConfig, ChainlitSection
    from boba_chainlit import UIOverride, ChainlitUiOverrideSection

Модули :mod:`boba_chainlit.app` и :mod:`boba_chainlit.__main__` — точки
входа приложения, их содержимое не реэкспортируется.
"""

from boba_chainlit.bridge import ChainlitBridgeSink
from boba_chainlit.config import ChainlitConfig, ChainlitSection
from boba_chainlit.files import save_upload
from boba_chainlit.session import ChatSession
from boba_chainlit.ui_overrides import (
    ChainlitUiOverrideSection,
    UIOverride,
    UIOverrideTomlConverter,
)

__all__ = [
    "ChainlitBridgeSink",
    "ChainlitConfig",
    "ChainlitSection",
    "ChainlitUiOverrideSection",
    "ChatSession",
    "UIOverride",
    "UIOverrideTomlConverter",
    "save_upload",
]
