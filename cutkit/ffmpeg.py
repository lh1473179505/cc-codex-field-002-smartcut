"""Thin subprocess wrapper around the system ffmpeg binary."""

from __future__ import annotations

import subprocess

from cutkit.errors import SmartCutError
from cutkit.probe import require_bin


def run_ffmpeg(args: list[str], *, timeout: int = 600) -> None:
    """Run ffmpeg, raising SmartCutError with a useful message on failure."""
    ffmpeg = require_bin("ffmpeg")
    cmd = [ffmpeg, "-y", "-hide_banner", "-nostdin", *args]
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise SmartCutError(f"ffmpeg timed out: {' '.join(cmd)}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        tail = "\n".join(detail.splitlines()[-8:])
        raise SmartCutError(f"ffmpeg failed ({proc.returncode}):\n{tail}")
