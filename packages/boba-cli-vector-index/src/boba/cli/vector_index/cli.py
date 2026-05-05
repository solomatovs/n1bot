"""boba-cli-vector-index: единый CLI поверх PipelineRegistry + admin."""

from __future__ import annotations

from boba.cli.vector_index.runner import VectorIndexCli

__all__ = ["main"]


def main() -> int:
    return VectorIndexCli().main()
