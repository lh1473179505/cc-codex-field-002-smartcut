from __future__ import annotations

from fractions import Fraction

import pytest

from cutkit.timecode import TimeParseError, parse_time, seconds_as_float


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("4/3", Fraction(4, 3)),
        ("5/2", Fraction(5, 2)),
        ("2.5", Fraction(5, 2)),
        ("00:00:01.5", Fraction(3, 2)),
        ("1:30", Fraction(90)),
        ("3", Fraction(3)),
    ],
)
def test_parse_time_formats(text, expected):
    assert parse_time(text) == expected


def test_parse_time_passthrough_and_float():
    assert parse_time(Fraction(1, 7)) == Fraction(1, 7)
    assert parse_time(2) == Fraction(2)
    assert seconds_as_float(Fraction(4, 3)) == 1.333333


def test_parse_time_rejects_garbage():
    with pytest.raises(TimeParseError):
        parse_time("not-a-time")
    with pytest.raises(TimeParseError):
        parse_time("")
