"""Exception types shared by cutkit commands."""

from __future__ import annotations


class CutkitError(RuntimeError):
    """Base class for expected, user-facing cutkit failures."""


class SmartCutError(CutkitError):
    """Raised when a smart-cut cannot be performed."""
