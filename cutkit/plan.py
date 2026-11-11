"""Frame-accurate window planning for the GOP-aware smart cut."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from cutkit.probe import packets


@dataclass(frozen=True)
class VideoLayout:
    """Packet-level layout of the source video stream."""

    stream_index: int
    time_base: Fraction
    frame_duration: Fraction
    nb_frames: int
    pts: list[int]
    keyframe_indices: list[int]
    duration: Fraction

    def frame_time(self, index: int) -> Fraction:
        return self.pts[index] * self.time_base

    def frame_end_time(self, index: int) -> Fraction:
        """Presentation time of the frame after ``index`` (handles EOF)."""
        if index < len(self.pts):
            return self.pts[index] * self.time_base
        return self.duration

    def index_at_or_before(self, t: Fraction) -> int:
        """Index of the last frame whose presentation time is <= t."""
        target = int((t / self.time_base))
        lo, hi = 0, len(self.pts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.pts[mid] <= target:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def index_at_or_after(self, t: Fraction) -> int:
        target = int(t / self.time_base)
        lo, hi = 0, len(self.pts) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if self.pts[mid] >= target:
                hi = mid
            else:
                lo = mid + 1
        return lo

    def keyframe_at_or_before(self, frame_index: int) -> int:
        lo, hi = 0, len(self.keyframe_indices) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.keyframe_indices[mid] <= frame_index:
                lo = mid
            else:
                hi = mid - 1
        return self.keyframe_indices[lo]


@dataclass(frozen=True)
class Segment:
    """A contiguous run of source frames handled by one ffmpeg invocation."""

    kind: str  # "reencode" or "copy"
    first_frame: int
    last_frame: int  # inclusive

    @property
    def frame_count(self) -> int:
        return self.last_frame - self.first_frame + 1


@dataclass(frozen=True)
class CutPlan:
    layout: VideoLayout
    start_frame: int
    end_frame: int  # exclusive
    segments: tuple[Segment, ...]

    @property
    def frame_count(self) -> int:
        return self.end_frame - self.start_frame


def _parse_fraction(value: Any) -> Fraction:
    if value is None:
        raise ValueError("missing rational value")
    text = str(value)
    if "/" in text:
        num, den = text.split("/", 1)
        return Fraction(int(num), int(den))
    return Fraction(text)


def inspect_video(path: str, stream_index: int, time_base: Fraction) -> VideoLayout:
    pkts = packets(path, stream_index=stream_index)
    pts: list[int] = []
    keyframes: list[int] = []
    for pkt in pkts:
        if pkt.get("pts") is None:
            continue
        pts.append(int(pkt["pts"]))
        if "K" in str(pkt.get("flags") or ""):
            keyframes.append(len(pts) - 1)
    if not pts:
        raise ValueError(f"no video packets found for stream {stream_index}")
    if not keyframes:
        raise ValueError("video stream has no keyframes; smart-cut needs seek points")
    deltas = {pts[i + 1] - pts[i] for i in range(len(pts) - 1)}
    if len(deltas) != 1:
        raise ValueError("variable frame-rate video is not supported by smart-cut")
    frame_duration = next(iter(deltas)) * time_base
    last_packet_duration = (
        _parse_fraction(pkts[-1]["duration"]) * time_base
        if pkts and pkts[-1].get("duration") is not None
        else frame_duration
    )
    duration = pts[-1] * time_base + last_packet_duration
    return VideoLayout(
        stream_index=stream_index,
        time_base=time_base,
        frame_duration=frame_duration,
        nb_frames=len(pts),
        pts=pts,
        keyframe_indices=keyframes,
        duration=duration,
    )


def build_plan(layout: VideoLayout, start: Fraction, end: Fraction) -> CutPlan:
    """Map the [start, end) window onto source frames and split it into segments.

    The window is half-open: it keeps frames with start_time <= pts < end.
    Re-encoded segments cover the partial GOPs at both edges; every complete
    GOP fully inside the window is kept as a bitstream-copy segment.
    """
    start_frame = layout.index_at_or_after(start)
    # last included frame is the last frame strictly before end
    end_frame = layout.index_at_or_before(end)
    if layout.frame_time(end_frame) >= end:
        end_frame -= 1
    end_frame += 1  # exclusive
    if start_frame >= end_frame:
        raise ValueError("window does not contain any video frame")

    # Classify the window into three parts:
    #   1. head  - frames before the first in-window keyframe (partial leading
    #      GOP), which must be re-encoded;
    #   2. whole - GOPs whose keyframe and following keyframe are both inside
    #      the window, bitstream-copied verbatim;
    #   3. tail  - the remaining partial trailing GOP, re-encoded.
    keyframes = layout.keyframe_indices
    raw: list[tuple[str, int, int]] = []

    first_in_window_kf = next((k for k in keyframes if k >= start_frame), None)
    if first_in_window_kf is None or first_in_window_kf >= end_frame:
        # No keyframe inside the window at all: one partial GOP to re-encode.
        raw.append(("reencode", start_frame, end_frame - 1))
        merged = [Segment("reencode", start_frame, end_frame - 1)]
        return CutPlan(layout, start_frame, end_frame, tuple(merged))

    if start_frame < first_in_window_kf:
        raw.append(("reencode", start_frame, first_in_window_kf - 1))

    # Consume whole GOPs: [kf, next_kf) only when next_kf <= end_frame.
    kf_pos = keyframes.index(first_in_window_kf)
    cursor = first_in_window_kf
    while kf_pos + 1 < len(keyframes) and keyframes[kf_pos + 1] <= end_frame:
        next_kf = keyframes[kf_pos + 1]
        raw.append(("copy", cursor, next_kf - 1))
        cursor = next_kf
        kf_pos += 1

    # The last GOP in the media has no following keyframe; it is whole only
    # when the window includes every frame up to the media's final frame.
    final_gop_complete = (
        kf_pos == len(keyframes) - 1 and end_frame == layout.nb_frames
    )
    if final_gop_complete:
        raw.append(("copy", cursor, end_frame - 1))
        cursor = end_frame

    if cursor < end_frame:
        if cursor in keyframes and end_frame - cursor == 1 and not raw:
            # The window is exactly one keyframe: copy it rather than render.
            raw.append(("copy", cursor, end_frame - 1))
        else:
            raw.append(("reencode", cursor, end_frame - 1))

    # Merge adjacent segments of the same kind.
    merged: list[Segment] = []
    for kind, first, last in raw:
        if merged and merged[-1].kind == kind and merged[-1].last_frame + 1 == first:
            merged[-1] = Segment(kind, merged[-1].first_frame, last)
        else:
            merged.append(Segment(kind, first, last))
    segments = merged

    return CutPlan(layout, start_frame, end_frame, tuple(segments))

