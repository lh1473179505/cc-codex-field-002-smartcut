"""Subtitle loading, window clipping and re-timing (SRT and ASS)."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from cutkit.errors import CutkitError
from cutkit.ffmpeg import run_ffmpeg
from cutkit.probe import container


@dataclass
class Cue:
    start: Fraction
    end: Fraction
    text: str


@dataclass
class SubtitleStats:
    kept: int = 0
    clipped: int = 0
    dropped: int = 0


def _parse_srt_clock(value: str) -> Fraction:
    hms, _, frac = value.strip().replace(",", ".").partition(".")
    parts = [int(p) for p in hms.split(":")]
    if len(parts) != 3:
        raise CutkitError(f"invalid SRT timestamp: {value}")
    hours, minutes, seconds = parts
    frac_value = Fraction(int(frac.ljust(3, "0")[:3]), 1000) if frac else Fraction(0)
    return Fraction(hours * 3600 + minutes * 60 + seconds) + frac_value


def _format_srt_clock(t: Fraction) -> str:
    total_ms = int((t * 1000).__round__())
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _parse_ass_clock(value: str) -> Fraction:
    hms, _, frac = value.strip().partition(".")
    hours, minutes, seconds = (int(p) for p in hms.split(":"))
    frac_value = Fraction(int(frac.ljust(2, "0")[:2]), 100) if frac else Fraction(0)
    return Fraction(hours * 3600 + minutes * 60 + seconds) + frac_value


def _format_ass_clock(t: Fraction) -> str:
    centis = int((t * 100).__round__())
    hours, rem = divmod(centis, 360_000)
    minutes, rem = divmod(rem, 6000)
    seconds, cs = divmod(rem, 100)
    return f"{hours:d}:{minutes:02d}:{seconds:02d}.{cs:02d}"


def parse_srt(path: Path) -> list[Cue]:
    content = path.read_text(encoding="utf-8-sig", errors="replace")
    cues: list[Cue] = []
    for block in content.replace("\r\n", "\n").replace("\r", "\n").split("\n\n"):
        lines = [line for line in block.split("\n") if line.strip() != ""]
        if len(lines) < 2:
            continue
        timing = next((line for line in lines if "-->" in line), None)
        if timing is None:
            continue
        head, _, tail = timing.partition("-->")
        text_lines = [line for line in lines if line is not timing and "-->" not in line]
        # Drop the numeric index line (first non-timing line when it is a bare int).
        if text_lines and text_lines[0].strip().isdigit():
            text_lines = text_lines[1:]
        try:
            cues.append(
                Cue(
                    start=_parse_srt_clock(head),
                    end=_parse_srt_clock(tail.strip().split()[0]),
                    text="\n".join(text_lines).strip(),
                )
            )
        except (ValueError, IndexError) as exc:
            raise CutkitError(f"invalid SRT cue in {path}: {exc}") from exc
    return cues


def parse_ass(path: Path) -> list[Cue]:
    cues: list[Cue] = []
    in_events = False
    format_fields: list[str] = []
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        if line.startswith("[Events]"):
            in_events = True
            continue
        if line.startswith("["):
            in_events = False
            continue
        if not in_events or not line:
            continue
        if line.lower().startswith("format:"):
            format_fields = [f.strip().lower() for f in line.split(":", 1)[1].split(",")]
            continue
        if line.lower().startswith("dialogue:") and format_fields:
            payload = line.split(":", 1)[1]
            # Text itself may contain commas; split only the structured fields.
            n_fields = len(format_fields)
            parts = payload.split(",", n_fields - 1)
            if len(parts) != n_fields:
                continue
            row = dict(zip(format_fields, parts[:-1], strict=False))
            try:
                cues.append(
                    Cue(
                        start=_parse_ass_clock(row["start"]),
                        end=_parse_ass_clock(row["end"]),
                        text=parts[-1].strip(),
                    )
                )
            except (KeyError, ValueError):
                continue
    return cues


def load_cues(path: Path) -> tuple[list[Cue], str]:
    suffix = path.suffix.lower()
    if suffix == ".srt":
        return parse_srt(path), "srt"
    if suffix in (".ass", ".ssa"):
        return parse_ass(path), "ass"
    raise CutkitError(f"unsupported subtitle format: {path} (use .srt, .ass or .ssa)")


def extract_embedded_srt(source: str, dest: Path) -> Path | None:
    """Extract the first embedded subtitle stream to an SRT file."""
    info = container(source)
    subtitle_indices = [
        int(stream["index"])
        for stream in info.get("streams") or []
        if stream.get("codec_type") == "subtitle"
    ]
    if not subtitle_indices:
        return None
    run_ffmpeg(["-i", source, "-map", f"0:{subtitle_indices[0]}", "-f", "srt", str(dest)])
    return dest


def clip_cues(
    cues: list[Cue], start: Fraction, end: Fraction
) -> tuple[list[Cue], SubtitleStats]:
    """Shift cues by -start, drop outside cues, clip cues crossing the edges."""
    stats = SubtitleStats()
    out: list[Cue] = []
    for cue in cues:
        if cue.end <= start or cue.start >= end:
            stats.dropped += 1
            continue
        clipped = cue.start < start or cue.end > end
        new_start = max(cue.start, start) - start
        new_end = min(cue.end, end) - start
        if new_end <= new_start:
            stats.dropped += 1
            continue
        out.append(Cue(new_start, new_end, cue.text))
        if clipped:
            stats.clipped += 1
        else:
            stats.kept += 1
    return out, stats


def write_srt(cues: list[Cue], dest: Path) -> Path:
    blocks = []
    for number, cue in enumerate(cues, start=1):
        blocks.append(
            f"{number}\n"
            f"{_format_srt_clock(cue.start)} --> {_format_srt_clock(cue.end)}\n"
            f"{cue.text}\n"
        )
    dest.write_text("\n".join(blocks), encoding="utf-8")
    return dest


def write_ass(cues: list[Cue], dest: Path) -> Path:
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        "PlayResX: 384\nPlayResY: 288\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
        "0,0,0,0,100,100,0,0,1,2,1,2,10,10,10,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, "
        "MarginV, Effect, Text\n"
    )
    lines = [
        f"Dialogue: 0,{_format_ass_clock(c.start)},{_format_ass_clock(c.end)},Default,,0,0,0,,{c.text}"
        for c in cues
    ]
    dest.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    return dest

