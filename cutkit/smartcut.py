"""GOP-aware, frame-accurate smart cut built on the system ffmpeg."""

from __future__ import annotations

import json
import tempfile
from fractions import Fraction
from pathlib import Path
from typing import Any

from cutkit.errors import CutkitError
from cutkit.ffmpeg import run_ffmpeg
from cutkit.plan import CutPlan, Segment, VideoLayout, build_plan, inspect_video
from cutkit.probe import container, packets, video_stream_index
from cutkit.subtitles import (
    Cue,
    SubtitleStats,
    clip_cues,
    extract_embedded_srt,
    load_cues,
    write_ass,
    write_srt,
)
from cutkit.timecode import seconds_as_float


def _ffmpeg_time(value: Fraction) -> str:
    # ffmpeg duration parsing wants decimals; 12 digits keeps 24 fps exact in
    # double precision while staying well within ffmpeg's internal precision.
    return f"{float(value):.12f}"


def _validate_input(path: Path) -> None:
    if not path.exists():
        raise CutkitError(f"input does not exist: {path}")
    if not path.is_file():
        raise CutkitError(f"input is not a file: {path}")


def _audio_stream_indices(info: dict[str, Any]) -> list[int]:
    return [
        int(stream["index"])
        for stream in info.get("streams") or []
        if stream.get("codec_type") == "audio"
    ]


def _render_segment(
    source: Path,
    layout: VideoLayout,
    segment: Segment,
    dest: Path,
) -> None:
    seek_frame = (
        segment.first_frame
        if segment.kind == "copy"
        else layout.keyframe_at_or_before(segment.first_frame)
    )
    seek_time = layout.frame_time(seek_frame)
    if segment.kind == "copy":
        duration = (
            layout.frame_end_time(segment.last_frame + 1)
            - layout.frame_time(segment.first_frame)
        )
        run_ffmpeg(
            [
                "-ss", _ffmpeg_time(seek_time),
                "-t", _ffmpeg_time(duration),
                "-i", str(source),
                "-map", f"0:{layout.stream_index}",
                "-an",
                "-c:v", "copy",
                str(dest),
            ]
        )
        return

    start_frame = segment.first_frame - seek_frame
    end_frame = segment.last_frame - seek_frame + 1
    run_ffmpeg(
        [
            "-ss", _ffmpeg_time(seek_time),
            "-i", str(source),
            "-map", f"0:{layout.stream_index}",
            "-an",
            "-vf",
            f"trim=start_frame={start_frame}:end_frame={end_frame},setpts=PTS-STARTPTS",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-g", "12",
            "-keyint_min", "12",
            "-bf", "0",
            "-fps_mode", "cfr",
            str(dest),
        ]
    )


def _render_audio(
    source: Path,
    audio_indices: list[int],
    start: Fraction,
    end: Fraction,
    dest: Path,
) -> None:
    if not audio_indices:
        return
    filter_parts: list[str] = []
    maps: list[str] = []
    for n, stream_index in enumerate(audio_indices):
        label = f"a{n}"
        filter_parts.append(
            f"[0:{stream_index}]atrim=start={_ffmpeg_time(start)}:end={_ffmpeg_time(end)},"
            f"asetpts=PTS-STARTPTS[{label}]"
        )
        maps.extend(["-map", f"[{label}]"])
    run_ffmpeg(
        [
            "-i", str(source),
            "-filter_complex", ";".join(filter_parts),
            *maps,
            "-c:a", "aac",
            "-ar", "48000",
            "-ac", "1",
            "-b:a", "64k",
            str(dest),
        ]
    )


def _load_and_clip_subtitles(
    source: Path,
    external: Path | None,
    start: Fraction,
    end: Fraction,
    workdir: Path,
) -> tuple[list[Cue], SubtitleStats, str]:
    if external is not None:
        if not external.is_file():
            raise CutkitError(f"subtitle file does not exist: {external}")
        cues, kind = load_cues(external)
        clipped, stats = clip_cues(cues, start, end)
        return clipped, stats, kind
    extracted = workdir / "embedded.srt"
    if extract_embedded_srt(str(source), extracted) is None:
        return [], SubtitleStats(), "none"
    cues, _ = load_cues(extracted)
    clipped, stats = clip_cues(cues, start, end)
    return clipped, stats, "srt"


def _segment_time_bounds(
    layout: VideoLayout, segment: Segment
) -> tuple[Fraction, Fraction]:
    return (
        layout.frame_time(segment.first_frame),
        layout.frame_end_time(segment.last_frame + 1),
    )


def _smart_render_report(plan: CutPlan) -> dict[str, Any]:
    layout = plan.layout

    def entry(segment: Segment | None) -> dict[str, float] | None:
        if segment is None:
            return None
        seg_start, seg_end = _segment_time_bounds(layout, segment)
        return {
            "src_start": seconds_as_float(seg_start),
            "src_end": seconds_as_float(seg_end),
        }

    head = plan.segments[0] if plan.segments[0].kind == "reencode" else None
    copy = next((s for s in plan.segments if s.kind == "copy"), None)
    tail = (
        plan.segments[-1]
        if len(plan.segments) > 1 and plan.segments[-1].kind == "reencode"
        else None
    )

    return {
        "head_reencode": entry(head),
        "copied": entry(copy),
        "tail_reencode": entry(tail),
    }


_MOV_FAMILIES = {".mp4", ".m4v", ".mov", ".3gp", ".3g2", ".mj2"}


def _mov_args(output: Path) -> list[str]:
    """Muxer flags for QuickTime-family outputs.

    A 12288 Hz video track timescale represents the common 24/25/30/48/60 fps
    frame durations exactly, avoiding jitter from the default 90 kHz tick.
    """
    if output.suffix.lower() not in _MOV_FAMILIES:
        return []
    return ["-video_track_timescale", "12288"]


def _probe_output(
    output: Path, audio_indices: list[int]
) -> dict[str, Any]:
    info = container(str(output))
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    video_duration = float(video.get("duration") or info["format"]["duration"])
    frame_pkts = packets(str(output), int(video["index"]))
    video_frames = len(frame_pkts)
    audio_report = []
    max_offset_ms = 0.0
    audio_streams = [s for s in info["streams"] if s["codec_type"] == "audio"]
    for output_index, _src_index in enumerate(audio_indices):
        # Audio streams are mapped in source order; pick the nth audio stream.
        if output_index >= len(audio_streams):
            raise CutkitError(
                f"output is missing audio stream {output_index}; expected {len(audio_indices)}"
            )
        stream = audio_streams[output_index]
        start_time = float(stream.get("start_time") or 0.0)
        duration = float(stream.get("duration") or info["format"]["duration"])
        # Relative alignment of this audio stream against the video window:
        # container start time plus any duration over/undershoot vs. video.
        delay_ms = (start_time + duration - video_duration) * 1000.0
        max_offset_ms = max(max_offset_ms, abs(delay_ms))
        audio_report.append(
            {
                "index": int(stream["index"]),
                "duration": round(duration, 6),
                "delay_vs_video_ms": round(delay_ms, 3),
            }
        )
    return {
        "video_duration": round(video_duration, 6),
        "video_frames": video_frames,
        "audio_streams": audio_report,
        "max_av_offset_ms": round(max_offset_ms, 3),
    }


def smart_cut(
    input_path: str,
    start: Fraction,
    duration: Fraction,
    output_path: str,
    qc_path: str,
    subtitles_path: str | None,
) -> dict[str, Any]:
    source = Path(input_path).resolve()
    output = Path(output_path).resolve()
    qc_file = Path(qc_path).resolve()
    external = Path(subtitles_path).resolve() if subtitles_path else None
    _validate_input(source)
    if duration <= 0:
        raise CutkitError("duration must be positive")
    end = start + duration

    info = container(str(source))
    video_index = video_stream_index(info)
    video_stream = next(s for s in info["streams"] if int(s["index"]) == video_index)
    stream_time_base = video_stream.get("time_base")
    if not stream_time_base:
        raise CutkitError("ffprobe did not report a video time_base")
    layout = inspect_video(str(source), video_index, Fraction(stream_time_base))
    if start < 0:
        raise CutkitError(f"start must be non-negative, got {start}")
    if end > layout.duration + layout.frame_duration / 2:
        raise CutkitError(
            f"window end {float(end):.6f}s exceeds media duration "
            f"{seconds_as_float(layout.duration)}s"
        )
    audio_indices = _audio_stream_indices(info)

    try:
        plan = build_plan(layout, start, end)
    except ValueError as exc:
        raise CutkitError(str(exc)) from exc

    output.parent.mkdir(parents=True, exist_ok=True)
    qc_file.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cutkit-") as tmp:
        workdir = Path(tmp)
        segment_files: list[Path] = []
        for n, segment in enumerate(plan.segments):
            # Raw H.264 carries no container timestamps, so the concat step
            # regenerates one uniform CFR timebase for every segment.
            dest = workdir / f"seg{n:02d}.h264"
            _render_segment(source, layout, segment, dest)
            segment_files.append(dest)

        concat_list = workdir / "concat.txt"
        concat_list.write_text(
            "".join(f"file '{path}'\n" for path in segment_files), encoding="utf-8"
        )

        audio_file = workdir / "audio.mp4"
        if audio_indices:
            _render_audio(source, audio_indices, start, end, audio_file)

        cues, sub_stats, sub_kind = _load_and_clip_subtitles(
            source, external, start, end, workdir
        )
        sub_file: Path | None = None
        if cues:
            suffix = ".ass" if sub_kind == "ass" else ".srt"
            sub_file = workdir / f"subtitles{suffix}"
            if sub_kind == "ass":
                write_ass(cues, sub_file)
            else:
                write_srt(cues, sub_file)

        fps = Fraction(
            layout.frame_duration.denominator,
            layout.frame_duration.numerator,
        )
        base_args = [
            "-f", "concat", "-safe", "0", "-r", _ffmpeg_time(fps),
            "-i", str(concat_list),
        ]
        if audio_indices:
            base_args += ["-i", str(audio_file)]
        map_args = ["-map", "0:v:0", "-c:v", "copy"]
        if audio_indices:
            map_args += ["-map", "1:a", "-c:a", "copy"]
        sidecar: Path | None = None
        ordered = sorted(cues, key=lambda cue: (cue.start, cue.end))
        overlapping = any(
            n > 0 and cue.start < ordered[n - 1].end for n, cue in enumerate(ordered)
        )
        if sub_file is not None and overlapping:
            # mov_text cannot represent overlapping cues and silently rewrites
            # their timestamps; keep timing exact in a same-name sidecar SRT.
            sidecar = write_srt(cues, output.with_suffix(".srt"))
            sub_file = None
        if sub_file is not None:
            # Inputs: 0 = concat video, 1 = audio (if any), subtitles come last.
            sub_input_index = 2 if audio_indices else 1
            try:
                run_ffmpeg(
                    [
                        *base_args,
                        "-i", str(sub_file),
                        *map_args,
                        "-map", f"{sub_input_index}:0",
                        "-c:s", "mov_text",
                        *_mov_args(output),
                        "-movflags", "+faststart",
                        str(output),
                    ]
                )
            except CutkitError:
                # Container cannot carry the subtitle (e.g. non-MP4 output):
                # mux AV only and write a same-name sidecar SRT.
                sidecar = output.with_suffix(".srt")
                write_srt(cues, sidecar)
                run_ffmpeg(
                    [
                        *base_args,
                        *map_args,
                        *_mov_args(output),
                        "-movflags", "+faststart",
                        str(output),
                    ]
                )
        else:
            run_ffmpeg(
                [
                    *base_args,
                    *map_args,
                    *_mov_args(output),
                    "-movflags", "+faststart",
                    str(output),
                ]
            )

        measured = _probe_output(output, audio_indices)

    qc = {
        "input": str(source),
        "output": str(output),
        "requested": {
            "start": seconds_as_float(start),
            "duration": seconds_as_float(duration),
            "end": seconds_as_float(end),
        },
        "actual": {
            "video_duration": measured["video_duration"],
            "video_frames": measured["video_frames"],
            "audio_streams": measured["audio_streams"],
        },
        "smart_render": _smart_render_report(plan),
        "subtitles": {
            "kept": sub_stats.kept,
            "clipped": sub_stats.clipped,
            "dropped": sub_stats.dropped,
            "sidecar": str(sidecar) if sidecar else None,
        },
        "sync": {"max_av_offset_ms": measured["max_av_offset_ms"]},
    }
    qc_file.write_text(json.dumps(qc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return qc

