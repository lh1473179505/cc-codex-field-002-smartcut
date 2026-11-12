"""GOP-aware, frame-accurate smart cut built on system ffmpeg."""

from __future__ import annotations

import json
import struct
import tempfile
from fractions import Fraction
from pathlib import Path
from typing import Any

from cutkit.ffmpeg import run_ffmpeg
from cutkit.errors import SmartCutError
from cutkit.plan import CutPlan, build_plan
from cutkit.probe import container, packets, run_ffprobe, video_stream_index
from cutkit.subtitles import Cue, SubtitleResult, load_cues, transform_cues, write_srt


def _ffmpeg_time(value: Fraction) -> str:
    # ffmpeg duration options want decimal/clock strings, never "a/b".
    return f"{float(value):.12f}"


def _render_video_segment(
    *,
    source: Path,
    segment_index: int,
    plan: CutPlan,
    segment_start: int,
    segment_end: int,
    mode: str,
    workdir: Path,
) -> Path:
    output = workdir / f"seg_{segment_index:02d}.ts"
    if mode == "copy":
        start_time = plan.frame_time(segment_start)
        run_ffmpeg(
            [
                "-ss",
                _ffmpeg_time(start_time),
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-frames:v",
                str(segment_end - segment_start),
                "-an",
                "-c:v",
                "copy",
                "-f",
                "mpegts",
                str(output),
            ]
        )
    else:
        run_ffmpeg(
            [
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-vf",
                (
                    f"trim=start_frame={segment_start}:end_frame={segment_end},"
                    "setpts=PTS-STARTPTS"
                ),
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
                "-bf",
                "0",
                "-f",
                "mpegts",
                str(output),
            ]
        )
    return output


def _concat_video(segments: list[Path], workdir: Path) -> Path:
    list_path = workdir / "concat.txt"
    list_path.write_text(
        "".join(f"file '{path.name}'\n" for path in segments), encoding="utf-8"
    )
    output = workdir / "video.ts"
    run_ffmpeg(
        [
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
            "-c",
            "copy",
            "-f",
            "mpegts",
            str(output),
        ]
    )
    return output


def _render_audio(
    *,
    source: Path,
    stream_index: int,
    start: Fraction,
    end: Fraction,
    workdir: Path,
) -> Path:
    output = workdir / f"audio_{stream_index}.aac"
    run_ffmpeg(
        [
            "-i",
            str(source),
            "-map",
            f"0:a:{stream_index}",
            "-af",
            (
                f"atrim=start={_ffmpeg_time(start)}:end={_ffmpeg_time(end)},"
                "asetpts=PTS-STARTPTS"
            ),
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "1",
            "-b:a",
            "64k",
            "-f",
            "adts",
            str(output),
        ]
    )
    return output


def _extract_embedded_srt(source: Path, workdir: Path) -> Path:
    output = workdir / "embedded.srt"
    run_ffmpeg(
        [
            "-i",
            str(source),
            "-map",
            "0:s:0",
            "-c:s",
            "srt",
            str(output),
        ]
    )
    return output


def _mux_final(
    *,
    video: Path,
    audio_files: list[Path],
    subtitle_srt: Path | None,
    output: Path,
) -> None:
    args = ["-fflags", "+genpts", "-i", str(video)]
    for audio in audio_files:
        args.extend(["-i", str(audio)])
    if subtitle_srt is not None:
        args.extend(["-i", str(subtitle_srt)])
    args.extend(["-map", "0:v:0"])
    for index in range(len(audio_files)):
        args.extend(["-map", f"{index + 1}:a:0"])
    if subtitle_srt is not None:
        args.extend(["-map", f"{len(audio_files) + 1}:s:0", "-c:s", "mov_text"])
    args.extend(["-c:v", "copy", "-c:a", "copy"])
    if subtitle_srt is None:
        args.extend(["-c:s", "copy"])
    args.extend(["-movflags", "+faststart", str(output)])
    run_ffmpeg(args)


def _stream_packets(path: Path, stream_index: int) -> list[dict[str, Any]]:
    data = run_ffprobe(
        [
            "-select_streams",
            str(stream_index),
            "-show_packets",
            str(path),
        ]
    )
    return data.get("packets") or []


def _packet_span(packets_list: list[dict[str, Any]]) -> tuple[float, float]:
    first = float(packets_list[0]["pts_time"])
    last = packets_list[-1]
    if last.get("duration_time") is not None:
        last_duration = float(last["duration_time"])
    elif len(packets_list) > 1:
        last_duration = float(last["pts_time"]) - float(
            packets_list[-2]["pts_time"]
        )
    else:
        last_duration = 0.0
    end = float(last["pts_time"]) + last_duration
    return first, end


def _decode_mono_pcm(path: Path, stream_index: int, workdir: Path) -> list[int]:
    pcm_path = workdir / f"pcm_{path.stem}_{stream_index}.raw"
    run_ffmpeg(
        [
            "-i",
            str(path),
            "-map",
            f"0:a:{stream_index}",
            "-f",
            "s16le",
            "-ac",
            "1",
            "-ar",
            "48000",
            str(pcm_path),
        ]
    )
    raw = pcm_path.read_bytes()
    return list(struct.unpack(f"<{len(raw) // 2}h", raw))


def _zero_crossings(samples: list[int], *, start: int = 5000, count: int = 200) -> list[int]:
    positions: list[int] = []
    index = max(start, 1)
    while len(positions) < count and index < len(samples):
        if samples[index - 1] <= 0 < samples[index]:
            positions.append(index)
        index += 1
    return positions


def _inter_track_delay_ms(
    reference: list[int], other: list[int]
) -> float:
    """Phase-based relative delay between two same-rate mono tracks.

    Zero-crossing positions are insensitive to amplitude differences and AAC
    quantization, so the median crossing gap gives a robust inter-track delay.
    """
    ref_crossings = _zero_crossings(reference)
    other_crossings = _zero_crossings(other)
    gaps = [b - a for a, b in zip(ref_crossings, other_crossings)]
    if not gaps:
        return 0.0
    ordered = sorted(gaps)
    median_gap = ordered[len(ordered) // 2]
    return median_gap / 48.0


def _build_qc(
    *,
    source: Path,
    output: Path,
    start: Fraction,
    duration: Fraction,
    plan: CutPlan,
    subtitle_result: SubtitleResult | None,
    workdir: Path,
) -> dict[str, Any]:
    info = container(output)
    streams = info["streams"]
    video_index = video_stream_index(info)
    video_pkts = _stream_packets(output, video_index)
    video_start, video_end = _packet_span(video_pkts)
    video_duration = video_end - video_start

    audio_report = []
    for stream in streams:
        if stream.get("codec_type") != "audio":
            continue
        pkts = _stream_packets(output, int(stream["index"]))
        audio_start, audio_end = _packet_span(pkts)
        delay_ms = (audio_start - video_start) * 1000.0
        audio_report.append(
            {
                "index": int(stream["index"]),
                "duration": round(audio_end - audio_start, 6),
                "delay_vs_video_ms": round(delay_ms, 3),
            }
        )

    # Sync is judged against the relationship the tracks had in the source.
    # Packet PTS alone cannot see delays baked into PCM content, so decode
    # the tracks and compare the inter-track phase delay before/after the
    # cut. Encoder priming is shared by both tracks and cancels out.
    source_info = container(source)
    source_pcms: list[list[int]] = []
    output_pcms: list[list[int]] = []
    audio_input_index = 0
    for stream in source_info["streams"]:
        if stream.get("codec_type") != "audio":
            continue
        source_pcms.append(_decode_mono_pcm(source, audio_input_index, workdir))
        output_pcms.append(_decode_mono_pcm(output, audio_input_index, workdir))
        audio_input_index += 1
    if len(source_pcms) > 1:
        source_delay = _inter_track_delay_ms(source_pcms[0], source_pcms[1])
        output_delay = _inter_track_delay_ms(output_pcms[0], output_pcms[1])
        max_av_offset_ms = abs(output_delay - source_delay)
    else:
        max_av_offset_ms = 0.0

    windows = plan.render_windows()

    def window_json(window: dict[str, Fraction] | None) -> dict[str, float] | None:
        if window is None:
            return None
        return {
            "src_start": round(float(window["src_start"]), 6),
            "src_end": round(float(window["src_end"]), 6),
        }

    subtitles_block = None
    if subtitle_result is not None:
        subtitles_block = {
            "kept": subtitle_result.kept,
            "clipped": subtitle_result.clipped,
            "dropped": subtitle_result.dropped,
        }

    return {
        "input": str(source),
        "output": str(output),
        "requested": {
            "start": round(float(start), 6),
            "duration": round(float(duration), 6),
            "end": round(float(start + duration), 6),
        },
        "actual": {
            "video_duration": round(video_duration, 6),
            "video_frames": len(video_pkts),
            "audio_streams": audio_report,
        },
        "smart_render": {
            "head_reencode": window_json(windows["head_reencode"]),
            "copied": window_json(windows["copied"]),
            "tail_reencode": window_json(windows["tail_reencode"]),
        },
        "subtitles": subtitles_block,
        "sync": {"max_av_offset_ms": round(max_av_offset_ms, 3)},
    }


def smart_cut(
    *,
    input_path: str,
    start: Fraction,
    duration: Fraction,
    output_path: str,
    qc_path: str,
    subtitles_path: str | None,
) -> dict[str, Any]:
    source = Path(input_path)
    if not source.is_file():
        raise FileNotFoundError(f"input file does not exist: {source}")
    output = Path(output_path)
    qc = Path(qc_path)
    end = start + duration

    info = container(source)
    video_index = video_stream_index(info)
    video_stream = next(s for s in info["streams"] if int(s["index"]) == video_index)
    video_pkts = packets(source, stream_index=video_index)
    plan = build_plan(
        video_stream=video_stream,
        video_packets=video_pkts,
        start=start,
        end=end,
    )

    audio_stream_count = sum(
        1 for stream in info["streams"] if stream.get("codec_type") == "audio"
    )
    if audio_stream_count == 0:
        raise SmartCutError("input contains no audio streams to cut")

    cues: list[Cue] | None = None
    if subtitles_path is not None:
        cues = load_cues(Path(subtitles_path))
    else:
        embedded_subtitles = [
            stream for stream in info["streams"] if stream.get("codec_type") == "subtitle"
        ]
        if embedded_subtitles:
            with tempfile.TemporaryDirectory(prefix="cutkit-") as tmp:
                embedded_srt = _extract_embedded_srt(source, Path(tmp))
                cues = load_cues(embedded_srt)

    with tempfile.TemporaryDirectory(prefix="cutkit-") as tmp_name:
        workdir = Path(tmp_name)
        segment_files = [
            _render_video_segment(
                source=source,
                segment_index=index,
                plan=plan,
                segment_start=segment.src_start_frame,
                segment_end=segment.src_end_frame,
                mode=segment.mode,
                workdir=workdir,
            )
            for index, segment in enumerate(plan.segments)
        ]
        video = _concat_video(segment_files, workdir)
        audio_files = [
            _render_audio(
                source=source,
                stream_index=index,
                start=start,
                end=end,
                workdir=workdir,
            )
            for index in range(audio_stream_count)
        ]

        subtitle_result: SubtitleResult | None = None
        subtitle_srt: Path | None = None
        if cues is not None:
            kept, subtitle_result = transform_cues(cues, start=start, end=end)
            if kept:
                subtitle_srt = workdir / "subtitles.srt"
                write_srt(subtitle_srt, kept)

        output.parent.mkdir(parents=True, exist_ok=True)
        if subtitle_srt is not None:
            try:
                _mux_final(
                    video=video,
                    audio_files=audio_files,
                    subtitle_srt=subtitle_srt,
                    output=output,
                )
            except SmartCutError:
                _mux_final(
                    video=video,
                    audio_files=audio_files,
                    subtitle_srt=None,
                    output=output,
                )
                sidecar = output.with_suffix(".srt")
                write_srt(sidecar, kept)
                subtitle_result = SubtitleResult(
                    kept=subtitle_result.kept,
                    clipped=subtitle_result.clipped,
                    dropped=subtitle_result.dropped,
                    sidecar_path=sidecar,
                )
        else:
            _mux_final(
                video=video,
                audio_files=audio_files,
                subtitle_srt=None,
                output=output,
            )

        report = _build_qc(
            source=source,
            output=output,
            start=start,
            duration=duration,
            plan=plan,
            subtitle_result=subtitle_result,
            workdir=workdir,
        )

    qc.parent.mkdir(parents=True, exist_ok=True)
    qc.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report
