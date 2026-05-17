"""Маппинг `AgentEvent` → UI: общий dispatcher + live/replay таргеты + мост."""

from boba.chainlit.agent.rendering.bridge import ChainlitBridgeSink
from boba.chainlit.agent.rendering.dispatcher import (
    AgentEventDispatcher,
    EventRenderTarget,
)
from boba.chainlit.agent.rendering.live import ChainlitLiveTarget
from boba.chainlit.agent.rendering.replay import (
    StepDictTarget,
    replay_history_to_steps,
    replay_history_to_steps_sync,
)

__all__ = [
    "AgentEventDispatcher",
    "ChainlitBridgeSink",
    "ChainlitLiveTarget",
    "EventRenderTarget",
    "StepDictTarget",
    "replay_history_to_steps",
    "replay_history_to_steps_sync",
]
