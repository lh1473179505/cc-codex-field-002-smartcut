"""Turn a requested time window into GOP-aware render segments.

A window is expressed in source frame numbers (frame-accurate). Complete
GOPs fully inside the window are marked for bitstream copy; the partial GOP
touching each window edge is marked for re-encoding.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from cutkit.errors import SmartCutError


@dataclass(frozen=True)
class Segment:
    """A contiguous run of source frames sharing one render strategy."""

    src_start_frame: int
    src_end_frame: int  # exclusive
    mode: str  # "reencode" or "copy"

    @property
    def frame_count(self) -> int:
        return self.src_end_frame - self.src_start_frame


@dataclass(frozen=True)
class CutPlan:
    start_frame: int
    end_frame: int  # exclusive
    frame_rate: Fraction
    segments: tuple[Segment, ...]

    @property
    def frame_count(self) -> int:
        return self.end_frame - self.start_frame

    def frame_time(self, frame: int) -> Fraction:
        return Fraction(frame, self.frame_rate)

    def _segment_window(self, segment: Segment) -> tuple[Fraction, Fraction]:
        return (
            self.frame_time(segment.src_start_frame),
            self.frame_time(segment.src_end_frame),
        )

    def render_windows(self) -> dict[str, dict[str, Fraction] | None]:
        """Return head/copy/tail source-time windows for the QC report."""
        result: dict[str, dict[str, Fraction] | None] = {
            "head_reencode": None,
            "copied": None,
            "tail_reencode": None,
        }
        reencodes = [segment for segment in self.segments if segment.mode == "reencode"]
        copies = [segment for segment in self.segments if segment.mode == "copy"]
        if reencodes:
            head = reencodes[0]
            start, end = self._segment_window(head)
            result["head_reencode"] = {"src_start": start, "src_end": end}
        if len(reencodes) > 1:
            tail = reencodes[-1]
            start, end = self._segment_window(tail)
            result["tail_reencode"] = {"src_start": start, "src_end": end}
        if copies:
            result["copied"] = {
                "src_start": self.frame_time(copies[0].src_start_frame),
                "src_end": self.frame_time(copies[-1].src_end_frame),
            }
        return result


def _frame_rate(video_stream: dict[str, Any]) -> Fraction:
    rate = video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate")
    if not rate or rate == "0/0":
        raise SmartCutError("could not determine video frame rate")
    num, den = rate.split("/")
    return Fraction(int(num), int(den))


def _time_base(video_stream: dict[str, Any]) -> Fraction:
    raw = video_stream.get("time_base")
    if not raw:
        raise SmartCutError("could not determine video stream time base")
    num, den = raw.split("/")
    return Fraction(int(num), int(den))


def build_plan(
    *,
    video_stream: dict[str, Any],
    video_packets: list[dict[str, Any]],
    start: Fraction,
    end: Fraction,
) -> CutPlan:
    """Compute frame boundaries and render segments for [start, end).

    The start frame is the first frame whose PTS is >= start; the end frame
    is the first frame whose PTS is >= end, so the output covers exactly the
    frames presented during the requested window.
    """
    frame_rate = _frame_rate(video_stream)
    time_base = _time_base(video_stream)
    pts_times = [
        int(pkt["pts"]) * time_base
        for pkt in video_packets
        if pkt.get("pts") is not None
    ]
    if not pts_times:
        raise SmartCutError("video stream has no readable packets")
    duration = pts_times[-1] + Fraction(1, frame_rate)
    if start < 0:
        raise SmartCutError("window start must not be negative")
    if end > duration:
        raise SmartCutError(
            f"window end {float(end):.6f}s exceeds media duration {float(duration):.6f}s"
        )

    start_frame = next((i for i, pts in enumerate(pts_times) if pts >= start), None)
    end_frame = next((i for i, pts in enumerate(pts_times) if pts >= end), None)
    if start_frame is None or end_frame is None or end_frame <= start_frame:
        raise SmartCutError("requested window does not contain any video frames")

    keyframe_frames = [
        i
        for i, pkt in enumerate(video_packets)
        if start_frame <= i < end_frame and "K" in str(pkt.get("flags") or "")
    ]
    # A GOP that ends exactly on the window boundary is still a complete GOP.
    boundary_keyframes = set(keyframe_frames)
    if (
        end_frame < len(video_packets)
        and "K" in str(video_packets[end_frame].get("flags") or "")
    ):
        boundary_keyframes.add(end_frame)

    # A segment is a complete, copyable GOP when it starts on a keyframe and
    # ends on the next keyframe (exclusive). A run that reaches the window
    # end mid-GOP is partial and must be re-encoded, even if it starts on a
    # keyframe.
    boundaries = [start_frame, *sorted(boundary_keyframes), end_frame]
    segments: list[Segment] = []
    for left, right in zip(boundaries, boundaries[1:]):
        if right <= left:
            continue
        is_complete_gop = left in boundary_keyframes and right in boundary_keyframes
        mode = "copy" if is_complete_gop else "reencode"
        segments.append(Segment(left, right, mode))

    return CutPlan(start_frame, end_frame, frame_rate, tuple(segments))
