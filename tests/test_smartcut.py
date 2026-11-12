from __future__ import annotations

import json
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

from cutkit.probe import container, packets, video_stream_index
from cutkit.smartcut import smart_cut
from cutkit.timecode import parse_time


def _run_cut(source_path: Path, srt_path: Path, tmp_path: Path, **overrides):
    output = tmp_path / "out.mp4"
    qc = tmp_path / "qc.json"
    params = {
        "input_path": str(source_path),
        "start": parse_time("4/3"),
        "duration": parse_time("5/2"),
        "output_path": str(output),
        "qc_path": str(qc),
        "subtitles_path": str(srt_path),
    }
    params.update(overrides)
    report = smart_cut(**params)
    return output, qc, report


def _vcl_nals(h264_path: Path) -> list[bytes]:
    data = h264_path.read_bytes()
    starts: list[tuple[int, int]] = []
    index = 0
    while index < len(data) - 3:
        if data[index : index + 4] == b"\x00\x00\x00\x01":
            starts.append((index, 4))
            index += 4
        elif data[index : index + 3] == b"\x00\x00\x01":
            starts.append((index, 3))
            index += 3
        else:
            index += 1
    nals = []
    for number, (position, prefix_len) in enumerate(starts):
        end = starts[number + 1][0] if number + 1 < len(starts) else len(data)
        payload = data[position + prefix_len : end]
        if payload and payload[0] & 0x1F in (1, 5):
            nals.append(payload)
    return nals


def test_end_to_end_window_and_qc(source_path, srt_path, tmp_path):
    output, qc, report = _run_cut(source_path, srt_path, tmp_path)
    assert output.is_file() and qc.is_file()
    loaded = json.loads(qc.read_text())
    assert loaded == report
    assert report["actual"]["video_frames"] == 60
    assert report["actual"]["video_duration"] == pytest.approx(2.5, abs=1 / 24)
    assert len(report["actual"]["audio_streams"]) == 2
    assert report["sync"]["max_av_offset_ms"] < 40


def test_output_pts_continuous_and_keyframes(source_path, srt_path, tmp_path):
    output, _, _ = _run_cut(source_path, srt_path, tmp_path)
    info = container(output)
    index = video_stream_index(info)
    packets_list = packets(output, stream_index=index)
    assert len(packets_list) == 60
    assert float(packets_list[0]["pts_time"]) == 0.0
    for previous, current in zip(packets_list, packets_list[1:]):
        delta = float(current["pts_time"]) - float(previous["pts_time"])
        assert delta == pytest.approx(1 / 24, abs=1e-6)
    keyframe_positions = [
        i for i, packet in enumerate(packets_list) if "K" in str(packet.get("flags"))
    ]
    assert keyframe_positions == [0, 4, 16, 28, 40, 52]


def test_middle_gop_is_bitstream_copy(source_path, srt_path, tmp_path):
    output, _, _ = _run_cut(source_path, srt_path, tmp_path)
    output_h264 = tmp_path / "out.h264"
    reference_h264 = tmp_path / "ref.h264"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(output),
         "-c:v", "copy", "-f", "h264", str(output_h264)],
        check=True,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", "1.5",
         "-i", str(source_path), "-frames:v", "48", "-an", "-c:v", "copy",
         "-f", "h264", str(reference_h264)],
        check=True,
    )
    output_nals = _vcl_nals(output_h264)
    reference_nals = _vcl_nals(reference_h264)
    assert len(output_nals) == 60
    assert len(reference_nals) == 48
    assert output_nals[4:52] == reference_nals
    assert output_nals[0] != reference_nals[0]


def test_first_frame_matches_source_frame(source_path, srt_path, tmp_path):
    output, _, _ = _run_cut(source_path, srt_path, tmp_path)
    source_raw = tmp_path / "src32.raw"
    output_raw = tmp_path / "out0.raw"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(source_path),
         "-vf", "trim=start_frame=32:end_frame=33", "-frames:v", "1",
         "-f", "rawvideo", "-pix_fmt", "yuv420p", str(source_raw)],
        check=True,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(output),
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "yuv420p", str(output_raw)],
        check=True,
    )
    import math

    source_bytes = source_raw.read_bytes()
    output_bytes = output_raw.read_bytes()
    assert len(source_bytes) == len(output_bytes)
    mse = sum((a - b) ** 2 for a, b in zip(source_bytes, output_bytes)) / len(source_bytes)
    psnr = 10 * math.log10(255**2 / mse)
    assert psnr > 35


def test_audio_tracks_preserved_and_aligned(source_path, srt_path, tmp_path):
    output, _, report = _run_cut(source_path, srt_path, tmp_path)
    streams = [s for s in container(output)["streams"] if s["codec_type"] == "audio"]
    assert len(streams) == 2
    assert all(s["codec_name"] == "aac" for s in streams)
    durations = [entry["duration"] for entry in report["actual"]["audio_streams"]]
    assert all(duration == pytest.approx(2.5, abs=0.1) for duration in durations)


def test_subtitles_muxed_or_sidecar(source_path, srt_path, tmp_path):
    output, _, report = _run_cut(source_path, srt_path, tmp_path)
    assert report["subtitles"] == {"kept": 5, "clipped": 3, "dropped": 3}
    has_embedded = any(
        stream["codec_type"] == "subtitle"
        for stream in container(output)["streams"]
    )
    sidecar = output.with_suffix(".srt")
    assert has_embedded or sidecar.is_file()


def test_missing_input_fails(tmp_path):
    from cutkit.errors import SmartCutError

    with pytest.raises(FileNotFoundError):
        smart_cut(
            input_path=str(tmp_path / "nope.mp4"),
            start=Fraction(0),
            duration=Fraction(1),
            output_path=str(tmp_path / "o.mp4"),
            qc_path=str(tmp_path / "q.json"),
            subtitles_path=None,
        )


def test_window_exceeding_duration_fails(source_path, srt_path, tmp_path):
    from cutkit.errors import SmartCutError

    with pytest.raises(SmartCutError):
        _run_cut(
            source_path,
            srt_path,
            tmp_path,
            start=Fraction(4),
            duration=Fraction(10),
        )
