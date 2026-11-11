"""Thin subprocess wrapper around the system ffmpeg binary.

Only the system ``ffmpeg`` is used — no Docker and no compiled bindings.
"""

from __future__ import annotations

import shutil
import subprocess

from cutkit.errors import CutkitError


def require_ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise CutkitError("ffmpeg not found on PATH")
    return path


def run_ffmpeg(args: list[str], *, timeout: int = 1800) -> None:
    """Run ffmpeg, raising CutkitError with the ffmpeg log on failure."""
    ffmpeg = require_ffmpeg()
    cmd = [ffmpeg, "-y", "-hide_banner", "-nostdin", "-loglevel", "error", *args]
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise CutkitError(f"ffmpeg timed out: {' '.join(cmd)}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise CutkitError(
            f"ffmpeg failed ({proc.returncode}): {detail or 'no error output'}"
        )

