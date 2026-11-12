"""Shared pytest fixtures for cutkit tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from cutkit.probe import container, packets, video_stream_index

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "fixtures" / "source.mp4"
SRT = ROOT / "fixtures" / "source.srt"


@pytest.fixture(scope="session")
def source_path() -> Path:
    if not FIXTURE.is_file():
        pytest.skip("fixtures/source.mp4 is missing")
    return FIXTURE


@pytest.fixture(scope="session")
def srt_path() -> Path:
    if not SRT.is_file():
        pytest.skip("fixtures/source.srt is missing")
    return SRT


@pytest.fixture()
def source_video(source_path: Path) -> tuple[dict, list[dict]]:
    info = container(source_path)
    index = video_stream_index(info)
    stream = next(s for s in info["streams"] if int(s["index"]) == index)
    return stream, packets(source_path, stream_index=index)
