"""Общие значения стендов исполнения инструментов."""

from boba.toolrun.stream_calls import StreamGroupsConfig

STREAM_CONFIG = StreamGroupsConfig(
    open_sec=30.0,
    stall_sec=60.0,
    poll_sec=0.2,
    pipe_bytes=65536,
    pipe_bytes_max=1 << 30,
)
"""Секция [stream_groups] стендов: сроки группы и буферы пайпов."""
