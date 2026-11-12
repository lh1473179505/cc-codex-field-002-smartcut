from __future__ import annotations

from fractions import Fraction

import pytest

from cutkit.subtitles import load_cues, parse_srt, transform_cues, write_srt


SRT_TEXT = """1
00:00:01,200 --> 00:00:01,800
SPANS START

2
00:00:03,500 --> 00:00:04,200
SPANS END

3
00:00:00,500 --> 00:00:01,200
BEFORE

4
00:00:04,000 --> 00:00:04,800
AFTER

5
00:00:02,000 --> 00:00:03,000
INSIDE
"""


def test_parse_srt_counts():
    cues = parse_srt(SRT_TEXT)
    assert len(cues) == 5
    assert cues[0].start == Fraction(12, 10)


def test_transform_drops_clips_and_shifts():
    cues = parse_srt(SRT_TEXT)
    kept, result = transform_cues(
        cues, start=Fraction(4, 3), end=Fraction(23, 6)
    )
    assert result.dropped == 2
    assert result.clipped == 2
    assert result.kept == 3
    by_text = {cue.text: cue for cue in kept}
    head = by_text["SPANS START"]
    assert head.start == 0
    assert float(head.end) == pytest.approx(1.8 - 4 / 3, abs=1e-9)
    tail = by_text["SPANS END"]
    assert float(tail.start) == pytest.approx(3.5 - 4 / 3, abs=1e-9)
    assert tail.end == Fraction(5, 2)
    inside = by_text["INSIDE"]
    assert float(inside.start) == pytest.approx(2 - 4 / 3, abs=1e-9)


def test_fixture_srt_boundaries(srt_path):
    cues = load_cues(srt_path)
    kept, result = transform_cues(
        cues, start=Fraction(4, 3), end=Fraction(23, 6)
    )
    assert (result.kept, result.clipped, result.dropped) == (5, 3, 3)
    times = [(float(cue.start), float(cue.end)) for cue in kept]
    assert times[0][0] == 0.0
    assert times[0][1] == pytest.approx(2.0 - 4 / 3, abs=1e-9)
    assert times[-1][0] == pytest.approx(3.5 - 4 / 3, abs=1e-9)
    assert times[-1][1] == 2.5


def test_write_srt_round_trip(tmp_path):
    cues = parse_srt(SRT_TEXT)
    kept, _ = transform_cues(cues, start=Fraction(4, 3), end=Fraction(23, 6))
    path = tmp_path / "out.srt"
    write_srt(path, kept)
    reloaded = parse_srt(path.read_text())
    assert len(reloaded) == len(kept)
    assert reloaded[0].start == 0
