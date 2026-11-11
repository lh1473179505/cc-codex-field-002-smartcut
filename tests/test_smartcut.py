from __future__ import annotations

import json
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

from cutkit.errors import CutkitError
from cutkit.probe import container, packets
from cutkit.smartcut import smart_cut
from cutkit.subtitles import parse_srt
from tests.conftest import SRT


def _ffprobe_json(*args: str) -> dict:
    proc = subprocess.run(
        ["ffprobe", "-hide_banner", "-loglevel", "error", *args, "-of", "json"],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


def _vcl_nals(path: Path) -> list[bytes]:
    data = path.read_bytes()
    starts: list[tuple[int, int]] = []
    j = 0
    while j < len(data) - 3:
        if data[j : j + 3] == b"\x00\x00\x01":
            starts.append((j, 3))
            j += 3
        elif j < len(data) - 4 and data[j : j + 4] == b"\x00\x00\x00\x01":
            starts.append((j, 4))
            j += 4
        else:
            j += 1
    nals = []
    for k, (pos, sc) in enumerate(starts):
        end = starts[k + 1][0] if k + 1 < len(starts) else len(data)
        payload = data[pos + sc : end]
        if payload and (payload[0] & 0x1F) in (1, 5):
            nals.append(payload)
    return nals


@pytest.fixture()
def result(tmp_path, source):
    output = tmp_path / "cut.mp4"
    qc_path = tmp_path / "qc.json"
    qc = smart_cut(
        input_path=str(source),
        start=Fraction(4, 3),
        duration=Fraction(5, 2),
        output_path=str(output),
        qc_path=str(qc_path),
        subtitles_path=str(SRT),
    )
    return output, qc_path, qc


def test_qc_report_fields(result):
    output, qc_path, qc = result
    assert qc_path.exists()
    on_disk = json.loads(qc_path.read_text())
    assert on_disk == qc
    assert qc["requested"] == pytest.approx(
        {"start": 4 / 3, "duration": 2.5, "end": 23 / 6}, abs=1e-6
    )
    assert qc["actual"]["video_frames"] == 60
    assert abs(qc["actual"]["video_duration"] - 2.5) <= 1 / 24
    assert qc["smart_render"]["head_reencode"] == pytest.approx(
        {"src_start": 4 / 3, "src_end": 1.5}
    )
    assert qc["smart_render"]["copied"] == pytest.approx(
        {"src_start": 1.5, "src_end": 3.5}
    )
    assert qc["smart_render"]["tail_reencode"] == pytest.approx(
        {"src_start": 3.5, "src_end": 23 / 6}
    )
    assert qc["subtitles"]["dropped"] == 3
    assert "max_av_offset_ms" in qc["sync"]


def test_output_frame_count_and_continuous_pts(result):
    output, _, _ = result
    data = _ffprobe_json(
        "-select_streams", "v:0", "-show_entries", "packet=pts_time,flags", str(output)
    )
    pkts = data["packets"]
    assert len(pkts) == 60
    pts = [float(p["pts_time"]) for p in pkts]
    for n, t in enumerate(pts):
        assert abs(t - n / 24) <= 1 / 24
    assert all(b > a for a, b in zip(pts, pts[1:]))
    keyframes = [n for n, p in enumerate(pkts) if "K" in p["flags"]]
    assert keyframes == [0, 4, 16, 28, 40, 52]


def test_middle_gop_is_bitstream_copy(tmp_path, source, result):
    output, _, _ = result
    src_mid = tmp_path / "src_mid.h264"
    out_raw = tmp_path / "out.h264"
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", "1.5", "-t", "2.0", "-i", str(source),
            "-an", "-c:v", "copy", str(src_mid),
        ],
        check=True,
    )
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(output), "-an", "-c:v", "copy", str(out_raw),
        ],
        check=True,
    )
    src_nals = _vcl_nals(src_mid)
    out_nals = _vcl_nals(out_raw)
    assert len(src_nals) == 48
    assert len(out_nals) == 60
    middle = out_nals[4:52]
    assert all(a == b for a, b in zip(src_nals, middle))
    # The re-encoded head must not be byte-identical to the source.
    assert out_nals[0] != src_nals[0]


def test_first_frame_matches_source_frame_32(tmp_path, source, result):
    output, _, _ = result
    out_png = tmp_path / "out0.png"
    src_png = tmp_path / "src32.png"
    wrong_png = tmp_path / "src33.png"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-i", str(output), "-frames:v", "1", str(out_png)], check=True
    )
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-ss", "1.3333333333", "-i", str(source), "-frames:v", "1", str(src_png)],
        check=True,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-ss", "1.375", "-i", str(source), "-frames:v", "1", str(wrong_png)],
        check=True,
    )

    def ssim(a: Path, b: Path) -> float:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", str(a), "-i", str(b),
             "-lavfi", "[0:v][1:v]ssim", "-f", "null", "-"],
            capture_output=True,
            text=True,
            check=True,
        )
        line = next(l for l in proc.stderr.splitlines() if "All:" in l)
        token = line.split("All:")[1].split()[0]
        return float(token.rstrip(")"))

    assert ssim(out_png, src_png) > 0.9
    assert ssim(out_png, src_png) > ssim(out_png, wrong_png)


def test_both_audio_streams_kept(result):
    output, _, qc = result
    info = container(str(output))
    audio = [s for s in info["streams"] if s["codec_type"] == "audio"]
    assert len(audio) == 2
    assert all(s["channels"] == 1 and s["codec_name"] == "aac" for s in audio)
    assert len(qc["actual"]["audio_streams"]) == 2
    for stream in qc["actual"]["audio_streams"]:
        assert abs(stream["duration"] - 2.5) <= 0.1


def test_audio_relative_delay_preserved(tmp_path, source):
    # A window covering the 100 ms pre-delay region makes the delay observable:
    # track 1 must stay silent for ~50 ms after a 50 ms window start.
    output = tmp_path / "delay.mp4"
    smart_cut(
        input_path=str(source),
        start=Fraction(1, 20),
        duration=Fraction(3, 10),
        output_path=str(output),
        qc_path=str(tmp_path / "delay-qc.json"),
        subtitles_path=None,
    )
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(output),
         "-map", "0:2", "-f", "f32le", "-"],
        capture_output=True,
        check=True,
    )
    import array

    samples = array.array("f")
    samples.frombytes(proc.stdout)
    first_audible = next(i for i, v in enumerate(samples) if abs(v) > 0.01)
    assert 40.0 < first_audible / 48.0 < 60.0


def test_subtitle_sidecar_timing(result, tmp_path):
    output, _, qc = result
    sidecar = Path(qc["subtitles"]["sidecar"])
    assert sidecar.exists()
    cues = parse_srt(sidecar)
    by_text = {c.text: c for c in cues}
    assert by_text["SPANS START AND STAYS INSIDE"].start == 0
    assert abs(
        float(by_text["SPANS START AND STAYS INSIDE"].end) - (1.8 - 4 / 3)
    ) < 0.001
    clipped_end = by_text["SPANS THE CUT END"]
    assert abs(float(clipped_end.start) - (3.5 - 4 / 3)) < 0.001
    assert abs(float(clipped_end.end) - 2.5) < 0.001
    assert "AFTER THE WINDOW" not in by_text
    assert "ENDS BEFORE THE CUT" not in by_text


def test_error_on_missing_input(tmp_path):
    with pytest.raises(CutkitError, match="does not exist"):
        smart_cut(
            input_path=str(tmp_path / "nope.mp4"),
            start=Fraction(0),
            duration=Fraction(1),
            output_path=str(tmp_path / "o.mp4"),
            qc_path=str(tmp_path / "q.json"),
            subtitles_path=None,
        )


def test_error_on_window_beyond_duration(tmp_path, source):
    with pytest.raises(CutkitError, match="exceeds media duration"):
        smart_cut(
            input_path=str(source),
            start=Fraction(4),
            duration=Fraction(2),
            output_path=str(tmp_path / "o.mp4"),
            qc_path=str(tmp_path / "q.json"),
            subtitles_path=None,
        )


def test_no_subtitles_when_container_has_none(tmp_path, source):
    # The fixture always has embedded subtitles; stripping them first proves
    # the no-subtitle path still produces a playable video-only-plus-audio file.
    stripped = tmp_path / "stripped.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(source),
         "-map", "0:v", "-map", "0:a", "-c", "copy", str(stripped)],
        check=True,
    )
    output = tmp_path / "nosub.mp4"
    qc = smart_cut(
        input_path=str(stripped),
        start=Fraction(4, 3),
        duration=Fraction(5, 2),
        output_path=str(output),
        qc_path=str(tmp_path / "q.json"),
        subtitles_path=None,
    )
    info = container(str(output))
    assert not [s for s in info["streams"] if s["codec_type"] == "subtitle"]
    assert qc["subtitles"] == {
        "kept": 0,
        "clipped": 0,
        "dropped": 0,
        "sidecar": None,
    }


def test_packet_count_helper_imports(result):
    # Guard against probe API drift: packets() must still work on outputs.
    output, _, _ = result
    assert len(packets(str(output), 0)) == 60


def test_window_running_to_media_end(tmp_path, source):
    output = tmp_path / "to-end.mp4"
    qc = smart_cut(
        input_path=str(source),
        start=Fraction(4, 3),
        duration=Fraction(11, 3),  # ends exactly at the 5.0 s media duration
        output_path=str(output),
        qc_path=str(tmp_path / "end-qc.json"),
        subtitles_path=None,
    )
    assert qc["actual"]["video_frames"] == 88
    assert qc["smart_render"]["copied"]["src_end"] == 5.0
    assert qc["smart_render"]["tail_reencode"] is None

