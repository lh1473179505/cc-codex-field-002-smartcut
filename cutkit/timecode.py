"""Time parsing helpers. Fractions are preferred so 24 fps boundaries stay exact."""

from __future__ import annotations

import re
from fractions import Fraction


_HMS = re.compile(
    r"^(?:(?P<h>\d+):)?(?P<m>\d+):(?P<s>\d+)(?:\.(?P<frac>\d+))?$"
)


class TimeParseError(ValueError):
    pass


def parse_time(value: str | float | int | Fraction) -> Fraction:
    """Parse a clock, decimal, or rational time string into a Fraction of seconds."""
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value, 1)
    if isinstance(value, float):
        return Fraction(value).limit_denominator(1_000_000)
    text = str(value).strip()
    if not text:
        raise TimeParseError("empty time string")
    if "/" in text and text.count("/") == 1 and ":" not in text:
        num, den = text.split("/")
        try:
            return Fraction(int(num), int(den))
        except (ValueError, ZeroDivisionError) as exc:
            raise TimeParseError(f"invalid rational time: {text}") from exc
    match = _HMS.match(text)
    if match:
        hours = int(match.group("h") or 0)
        minutes = int(match.group("m"))
        seconds = int(match.group("s"))
        frac = match.group("frac") or "0"
        frac_value = Fraction(int(frac), 10 ** len(frac))
        return Fraction(hours * 3600 + minutes * 60 + seconds) + frac_value
    try:
        if "." in text:
            return Fraction(text).limit_denominator(1_000_000)
        return Fraction(int(text), 1)
    except ValueError as exc:
        raise TimeParseError(f"invalid time: {text}") from exc


def seconds_as_float(value: Fraction, ndigits: int = 6) -> float:
    return round(float(value), ndigits)
