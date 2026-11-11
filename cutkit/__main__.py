from __future__ import annotations

import argparse
import json
import sys

from cutkit.probe import ProbeError, container, keyframe_pts
from cutkit.timecode import TimeParseError, parse_time, seconds_as_float


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cutkit",
        description="Media probe and smart-cut toolkit.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    probe_cmd = sub.add_parser("probe", help="Print container / stream / keyframe info as JSON")
    probe_cmd.add_argument("input", help="Media file path")
    probe_cmd.add_argument(
        "--keyframes",
        action="store_true",
        help="Also list video keyframe PTS in seconds",
    )

    cut_cmd = sub.add_parser(
        "smart-cut",
        help="GOP-aware, frame-accurate cut (not implemented yet)",
    )
    cut_cmd.add_argument("--input", "-i", required=True, help="Source media file")
    cut_cmd.add_argument("--start", required=True, help="Window start, e.g. 4/3 or 00:00:01.333")
    cut_cmd.add_argument("--duration", required=True, help="Window duration, e.g. 5/2 or 2.5")
    cut_cmd.add_argument("--output", "-o", required=True, help="Output MP4 path")
    cut_cmd.add_argument("--qc", required=True, help="QC report JSON path")
    cut_cmd.add_argument(
        "--subtitles",
        help="Optional external SRT/ASS file; otherwise use embedded subtitle streams",
    )
    return parser


def cmd_probe(args: argparse.Namespace) -> int:
    info = container(args.input)
    if args.keyframes:
        info["keyframes_pts"] = keyframe_pts(args.input)
    json.dump(info, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


def cmd_smart_cut(args: argparse.Namespace) -> int:
    # Validate times early so a typo fails fast; the cut itself is still TODO.
    start = parse_time(args.start)
    duration = parse_time(args.duration)
    if duration <= 0:
        raise TimeParseError("duration must be positive")
    _ = (start, args.input, args.output, args.qc, args.subtitles)
    raise NotImplementedError(
        "smart-cut is not implemented in this snapshot. "
        f"Parsed start={seconds_as_float(start)}s duration={seconds_as_float(duration)}s"
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.cmd == "probe":
            return cmd_probe(args)
        if args.cmd == "smart-cut":
            return cmd_smart_cut(args)
        parser.error(f"unknown command: {args.cmd}")
    except (ProbeError, TimeParseError, NotImplementedError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
