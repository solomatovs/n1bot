"""Shell-инструмент без изоляции: доступ к ФС и сети как у агента."""

from boba.chainlit2.agent.tools.shell.config import BashLocalConfig
from boba.chainlit2.agent.tools.shell.tools import build_bash_local_tool

__all__ = ["BashLocalConfig", "build_bash_local_tool"]
