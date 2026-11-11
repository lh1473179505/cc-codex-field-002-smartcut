# cutkit

A small media toolkit built around system `ffmpeg` / `ffprobe`.

The `probe` command already works and is the supported way to inspect fixtures in this repository. The `smart-cut` command is the intended next piece of the toolkit: a GOP-aware, frame-accurate cutter that can keep complete GOPs by bitstream copy and only re-encode incomplete GOPs at the window edges, while keeping audio and subtitles aligned to the same window.

This snapshot does **not** implement `smart-cut` yet. The CLI entry point and time/probe helpers are in place so work can start from a real package rather than an empty folder.

## Requirements

- Python 3.11+ (stdlib only)
- `ffmpeg` and `ffprobe` on `PATH`

No containers are used. Do not add Docker files.

## Usage

```bash
python -m cutkit probe --keyframes fixtures/source.mp4
```

`fixtures/source.mp4` is a checked-in 5 second test clip. See `fixtures/README.md` for its layout (frame rate, GOP, audio tracks, subtitles).

## Layout

- `cutkit/probe.py` — ffprobe wrappers (`container`, `packets`, `frames`, `keyframe_pts`)
- `cutkit/timecode.py` — clock / decimal / rational time parsing
- `cutkit/__main__.py` — `probe` and a stub `smart-cut` interface
- `fixtures/` — source media used for local work
- `scripts/generate_fixtures.sh` — regenerates the fixture from lavfi sources (should be bit-identical given the same ffmpeg build)
