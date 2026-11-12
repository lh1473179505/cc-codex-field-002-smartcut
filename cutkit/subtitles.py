"""External/embedded subtitle loading, window clipping and SRT writing.

Only SRT and (basic) ASS inputs are supported, which matches the fixtures
and keeps the toolkit stdlib-only. Cue timing is handled with Fraction so
frame-aligned boundaries stay exact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from cutkit.errors import SmartCutError


@dataclass
class Cue:
    start: Fraction
    end: Fraction
    text: str


@dataclass(frozen=True)
class SubtitleResult:
    kept: int
    clipped: int
    dropped: int
    sidecar_path: Path | None


def _parse_clock(value: str) -> Fraction:
    value = value.strip().replace(",", ".")
    parts = value.split(":")
    if len(parts) == 3:
        hours, minutes, seconds = parts
        return Fraction(int(hours)) * 3600 + Fraction(int(minutes)) * 60 + Fraction(seconds)
    if len(parts) == 2:
        minutes, seconds = parts
        return Fraction(int(minutes)) * 60 + Fraction(seconds)
    return Fraction(value)


def _format_srt_time(value: Fraction) -> str:
    total_ms = int((value * 1000).__round__())
    hours, rem = divmod(max(total_ms, 0), 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def parse_srt(content: str) -> list[Cue]:
    cues: list[Cue] = []
    blocks = re.split(r"\n{2,}", content.replace("\r\n", "\n").strip())
    arrow = re.compile(r"\s*-->\s*")
    for block in blocks:
        lines = [line for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        timing_idx = next((i for i, line in enumerate(lines) if arrow.search(line)), None)
        if timing_idx is None:
            continue
        raw_start, raw_end = arrow.split(lines[timing_idx], maxsplit=1)
        cue_text = "\n".join(lines[timing_idx + 1 :]).strip()
        cues.append(Cue(_parse_clock(raw_start), _parse_clock(raw_end.split()[0]), cue_text))
    return cues


def parse_ass(content: str) -> list[Cue]:
    cues: list[Cue] = []
    in_events = False
    dialogue_key = "dialogue"
    start_idx = end_idx = text_idx = -1
    for raw_line in content.replace("\r\n", "\n").split("\n"):
        line = raw_line.strip()
        if line.lower().startswith("[events]"):
            in_events = True
            continue
        if line.startswith("["):
            in_events = False
        if not in_events or not line:
            continue
        if line.lower().startswith("format:"):
            fields = [field.strip().lower() for field in line.split(":", 1)[1].split(",")]
            start_idx = fields.index("start")
            end_idx = fields.index("end")
            text_idx = fields.index("text")
            continue
        if line.lower().startswith(f"{dialogue_key}:"):
            if text_idx < 0:
                raise SmartCutError("ASS file is missing a Format line in [Events]")
            payload = line.split(":", 1)[1]
            fields = payload.split(",", text_idx)
            if len(fields) <= text_idx:
                continue
            cues.append(
                Cue(
                    _parse_clock(fields[start_idx]),
                    _parse_clock(fields[end_idx]),
                    fields[text_idx].strip(),
                )
            )
    if not cues:
        raise SmartCutError("ASS subtitle file contains no Dialogue events")
    return cues


def load_cues(path: Path) -> list[Cue]:
    suffix = path.suffix.lower()
    content = path.read_text(encoding="utf-8-sig")
    if suffix == ".srt":
        cues = parse_srt(content)
    elif suffix in {".ass", ".ssa"}:
        cues = parse_ass(content)
    else:
        raise SmartCutError(f"unsupported subtitle format: {path.suffix} (use .srt or .ass)")
    if not cues:
        raise SmartCutError(f"no cues found in subtitle file: {path}")
    return cues


def transform_cues(
    cues: list[Cue],
    *,
    start: Fraction,
    end: Fraction,
) -> tuple[list[Cue], SubtitleResult]:
    """Shift cues by -start, drop outside cues, and clip boundary cues."""
    kept: list[Cue] = []
    clipped = 0
    dropped = 0
    for cue in cues:
        if cue.end <= start or cue.start >= end:
            dropped += 1
            continue
        was_clipped = cue.start < start or cue.end > end
        clipped_start = max(cue.start, start) - start
        clipped_end = min(cue.end, end) - start
        if clipped_end <= clipped_start:
            dropped += 1
            continue
        kept.append(Cue(clipped_start, clipped_end, cue.text))
        if was_clipped:
            clipped += 1
    result = SubtitleResult(kept=len(kept), clipped=clipped, dropped=dropped, sidecar_path=None)
    return kept, result


def write_srt(path: Path, cues: list[Cue]) -> None:
    chunks = []
    for index, cue in enumerate(cues, start=1):
        chunks.append(
            f"{index}\n{_format_srt_time(cue.start)} --> {_format_srt_time(cue.end)}\n{cue.text}"
        )
    path.write_text("\n\n".join(chunks) + "\n", encoding="utf-8")
