"""Thin wrappers around ffprobe. This module is already used by the probe CLI."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


class ProbeError(RuntimeError):
    pass


def require_bin(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise ProbeError(f"{name} not found on PATH")
    return path


def run_ffprobe(args: list[str], *, timeout: int = 60) -> dict[str, Any]:
    ffprobe = require_bin("ffprobe")
    cmd = [ffprobe, "-hide_banner", "-loglevel", "error", "-print_format", "json", *args]
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"ffprobe timed out: {' '.join(cmd)}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise ProbeError(f"ffprobe failed ({proc.returncode}): {detail}")
    if not proc.stdout.strip():
        raise ProbeError("ffprobe returned empty JSON")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"ffprobe returned invalid JSON: {exc}") from exc


def container(path: str | Path) -> dict[str, Any]:
    return run_ffprobe(
        [
            "-show_format",
            "-show_streams",
            str(path),
        ]
    )


def packets(path: str | Path, stream_index: int | None = None) -> list[dict[str, Any]]:
    args = ["-show_packets", str(path)]
    if stream_index is not None:
        args = ["-select_streams", str(stream_index), *args]
    data = run_ffprobe(args)
    return data.get("packets") or []


def frames(path: str | Path, stream_index: int | None = None) -> list[dict[str, Any]]:
    args = ["-show_frames", str(path)]
    if stream_index is not None:
        args = ["-select_streams", str(stream_index), *args]
    data = run_ffprobe(args)
    return data.get("frames") or []


def video_stream_index(info: dict[str, Any]) -> int:
    for stream in info.get("streams") or []:
        if stream.get("codec_type") == "video":
            return int(stream["index"])
    raise ProbeError("no video stream found")


def keyframe_pts(path: str | Path, stream_index: int | None = None) -> list[float]:
    """Return presentation timestamps (seconds) of video keyframe packets, in order."""
    info = container(path)
    idx = stream_index if stream_index is not None else video_stream_index(info)
    pts_list: list[float] = []
    for pkt in packets(path, stream_index=idx):
        flags = str(pkt.get("flags") or "")
        # ffprobe uses "K" / "K_" for keyframes
        if "K" not in flags:
            continue
        if pkt.get("pts_time") is None:
            continue
        pts_list.append(float(pkt["pts_time"]))
    return pts_list
