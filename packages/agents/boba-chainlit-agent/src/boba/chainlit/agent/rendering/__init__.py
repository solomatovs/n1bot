"""Маппинг `AgentEvent` → UI: общий dispatcher + live/replay таргеты + мост."""

from boba.chainlit.agent.rendering.bridge import ChainlitBridgeSink
from boba.chainlit.agent.rendering.dispatcher import (
    AgentEventDispatcher,
    EventRenderTarget,
)
from boba.chainlit.agent.rendering.live import ChainlitLiveTarget
from boba.chainlit.agent.rendering.replay import (
    StepDictTarget,
    ThreadContent,
    replay_history_to_thread,
    replay_history_to_thread_sync,
)

__all__ = [
    "AgentEventDispatcher",
    "ChainlitBridgeSink",
    "ChainlitLiveTarget",
    "EventRenderTarget",
    "StepDictTarget",
    "ThreadContent",
    "replay_history_to_thread",
    "replay_history_to_thread_sync",
]
