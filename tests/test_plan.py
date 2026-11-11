from __future__ import annotations

from fractions import Fraction

import pytest

from cutkit.plan import build_plan, inspect_video


@pytest.fixture(scope="module")
def layout(source):
    return inspect_video(str(source), 0, Fraction(1, 12288))


def kinds(plan):
    return [(s.kind, s.first_frame, s.last_frame) for s in plan.segments]


def test_main_window_matches_spec_segments(layout):
    plan = build_plan(layout, Fraction(4, 3), Fraction(23, 6))
    assert (plan.start_frame, plan.end_frame) == (32, 92)
    assert plan.frame_count == 60
    assert kinds(plan) == [
        ("reencode", 32, 35),
        ("copy", 36, 83),
        ("reencode", 84, 91),
    ]


def test_keyframe_aligned_window_is_fully_copied(layout):
    plan = build_plan(layout, Fraction(3, 2), Fraction(7, 2))
    assert kinds(plan) == [("copy", 36, 83)]


def test_start_on_keyframe_whole_gop_in_window_is_copied(layout):
    plan = build_plan(layout, Fraction(2), Fraction(3))
    # GOP 48..59 is fully inside the window, so it must be stream-copied even
    # though it is the last GOP the window reaches.
    assert kinds(plan) == [("copy", 48, 71)]


def test_window_within_single_gop_reencodes_exact_window(layout):
    plan = build_plan(layout, Fraction(26, 24), Fraction(34, 24))
    # No keyframe falls inside this window, so all requested frames are
    # re-encoded; rendering seeks back to the preceding keyframe.
    assert kinds(plan) == [("reencode", 26, 33)]


def test_end_exactly_on_keyframe_copies_complete_gops(layout):
    plan = build_plan(layout, Fraction(4, 3), Fraction(2))
    assert kinds(plan) == [
        ("reencode", 32, 35),
        ("copy", 36, 47),
    ]


def test_single_keyframe_window_is_copied(layout):
    plan = build_plan(layout, Fraction(3, 2), Fraction(3, 2) + Fraction(1, 24))
    assert kinds(plan) == [("copy", 36, 36)]


def test_window_reaching_media_end_copies_final_gop(layout):
    plan = build_plan(layout, Fraction(9, 2), Fraction(5))
    assert kinds(plan) == [("copy", 108, 119)]


def test_partial_head_then_copy_to_media_end(layout):
    plan = build_plan(layout, Fraction(4, 3), Fraction(5))
    assert plan.frame_count == 88
    assert kinds(plan) == [
        ("reencode", 32, 35),
        ("copy", 36, 119),
    ]


def test_segments_are_contiguous_and_cover_window(layout):
    plan = build_plan(layout, Fraction(4, 3), Fraction(23, 6))
    frames: list[int] = []
    for segment in plan.segments:
        frames.extend(range(segment.first_frame, segment.last_frame + 1))
    assert frames == list(range(32, 92))

