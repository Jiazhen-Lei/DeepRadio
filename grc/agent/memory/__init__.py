"""User-selected MainAgent presentation settings."""

from __future__ import annotations


def __getattr__(name):
    if name in ("UserProfile", "STYLE_GUIDE", "LANGUAGE_GUIDE"):
        from . import profile
        return getattr(profile, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
