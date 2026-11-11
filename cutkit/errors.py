"""Exception types shared by cutkit commands."""

from __future__ import annotations


class CutkitError(RuntimeError):
    """Raised for any expected smart-cut failure (printed on stderr, exit 1)."""

