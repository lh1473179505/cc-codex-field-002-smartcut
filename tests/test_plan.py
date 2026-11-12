from __future__ import annotations

from fractions import Fraction

import pytest

from cutkit.errors import SmartCutError
from cutkit.plan import build_plan


def test_window_matches_requested_frames(source_video):
    stream, video_packets = source_video
    plan = build_plan(
        video_stream=stream,
        video_packets=video_packets,
        start=Fraction(4, 3),
        end=Fraction(23, 6),
    )
    assert plan.start_frame == 32
    assert plan.end_frame == 92
    assert plan.frame_count == 60


def test_head_copy_tail_segments(source_video):
    stream, video_packets = source_video
    plan = build_plan(
        video_stream=stream,
        video_packets=video_packets,
        start=Fraction(4, 3),
        end=Fraction(23, 6),
    )
    modes = [(segment.src_start_frame, segment.src_end_frame, segment.mode) for segment in plan.segments]
    assert (32, 36, "reencode") in modes
    assert (84, 92, "reencode") in modes
    copies = [segment for segment in plan.segments if segment.mode == "copy"]
    assert copies[0].src_start_frame == 36
    assert copies[-1].src_end_frame == 84
    assert sum(segment.frame_count for segment in copies) == 48


def test_render_windows_cover_full_range(source_video):
    stream, video_packets = source_video
    plan = build_plan(
        video_stream=stream,
        video_packets=video_packets,
        start=Fraction(4, 3),
        end=Fraction(23, 6),
    )
    windows = plan.render_windows()
    assert windows["head_reencode"] == {
        "src_start": Fraction(4, 3),
        "src_end": Fraction(3, 2),
    }
    assert windows["copied"] == {
        "src_start": Fraction(3, 2),
        "src_end": Fraction(7, 2),
    }
    assert windows["tail_reencode"] == {
        "src_start": Fraction(7, 2),
        "src_end": Fraction(23, 6),
    }


def test_window_inside_single_gop_is_all_reencode(source_video):
    stream, video_packets = source_video
    plan = build_plan(
        video_stream=stream,
        video_packets=video_packets,
        start=Fraction(1, 24),
        end=Fraction(3, 24),
    )
    assert [(segment.src_start_frame, segment.mode) for segment in plan.segments] == [
        (1, "reencode")
    ]
    windows = plan.render_windows()
    assert windows["copied"] is None
    assert windows["tail_reencode"] is None


def test_keyframe_aligned_window_is_all_copy(source_video):
    stream, video_packets = source_video
    plan = build_plan(
        video_stream=stream,
        video_packets=video_packets,
        start=Fraction(1, 2),
        end=Fraction(1, 1),
    )
    assert all(segment.mode == "copy" for segment in plan.segments)
    assert plan.render_windows()["head_reencode"] is None


def test_window_past_duration_is_rejected(source_video):
    stream, video_packets = source_video
    with pytest.raises(SmartCutError):
        build_plan(
            video_stream=stream,
            video_packets=video_packets,
            start=Fraction(4),
            end=Fraction(10),
        )
