"""Entrypoint chainlit-приложения. Делегирует в composition.main()."""

from __future__ import annotations

from boba.chainlit.composition import main

__all__ = ["main"]


if __name__ == "__main__":
    main()
