from __future__ import annotations

from fractions import Fraction

from cutkit.subtitles import (
    Cue,
    clip_cues,
    parse_srt,
    write_srt,
)
from tests.conftest import SRT


def _cues():
    return [
        Cue(Fraction(12, 10), Fraction(18, 10), "A"),       # spans start
        Cue(Fraction(35, 10), Fraction(42, 10), "B"),       # spans end
        Cue(Fraction(5, 10), Fraction(12, 10), "EDGE"),     # ends on start -> drop
        Cue(Fraction(5, 10), Fraction(11, 10), "BEFORE"),   # fully before
        Cue(Fraction(40, 10), Fraction(48, 10), "AFTER"),   # starts on end -> drop
        Cue(Fraction(2), Fraction(3), "INSIDE"),            # fully inside
    ]


def test_clip_shift_and_drop():
    start, end = Fraction(4, 3), Fraction(23, 6)
    out, stats = clip_cues(_cues(), start, end)
    by_text = {c.text: c for c in out}
    assert set(by_text) == {"A", "B", "INSIDE"}
    assert by_text["A"].start == 0
    assert by_text["A"].end == Fraction(18, 10) - start
    assert by_text["B"].start == Fraction(35, 10) - start
    assert by_text["B"].end == end - start
    assert by_text["INSIDE"].start == Fraction(2) - start
    assert (stats.kept, stats.clipped, stats.dropped) == (1, 2, 3)


def test_fixture_srt_boundary_examples():
    cues = parse_srt(SRT)
    out, stats = clip_cues(cues, Fraction(4, 3), Fraction(23, 6))
    # cue 4 (1.2 -> 1.8) becomes 0.0 -> 0.4667 in output time
    cue4 = next(c for c in out if c.text == "SPANS START AND STAYS INSIDE")
    assert cue4.start == 0
    assert abs(float(cue4.end) - (1.8 - 4 / 3)) < 1e-9
    # cue 7 (3.5 -> 4.2) clipped to 2.1667 -> 2.5
    cue7 = next(c for c in out if c.text == "SPANS THE CUT END")
    assert abs(float(cue7.start) - (3.5 - 4 / 3)) < 1e-9
    assert cue7.end == Fraction(5, 2)
    # cues 2 and 8 land fully outside
    assert stats.dropped == 3
    assert all(c.text not in {"ENDS BEFORE THE CUT", "AFTER THE WINDOW"} for c in out)


def test_srt_roundtrip(tmp_path):
    cues = [
        Cue(Fraction(0), Fraction(7, 15), "LINE1\nLINE2")
    ]
    dest = write_srt(cues, tmp_path / "out.srt")
    parsed = parse_srt(dest)
    assert len(parsed) == 1
    assert parsed[0].start == 0
    assert abs(float(parsed[0].end) - 7 / 15) < 0.001
    assert parsed[0].text == "LINE1\nLINE2"

