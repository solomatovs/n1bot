from typing import Literal

from langchain.tools import tool

__all__ = [
    "get_weather",
]


@tool
def get_weather(city: Literal["nyc", "sf"]):
    """Use this to get weather information."""
    if city == "nyc":
        return "It might be cloudy in nyc"
    if city == "sf":
        return "It's always sunny in sf"
    raise AssertionError("Unknown city")
