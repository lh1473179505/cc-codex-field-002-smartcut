# cutkit

A small media toolkit built around system `ffmpeg` / `ffprobe`. It provides a
working `probe` command and a GOP-aware, frame-accurate `smart-cut` command
that keeps complete GOPs by bitstream copy and only re-encodes the partial GOPs
at the window edges, while keeping every audio track and the subtitles aligned
to the same window.

## Requirements

- Python 3.11+ (stdlib only)
- `ffmpeg` and `ffprobe` on `PATH`

No containers are used. Do not add Docker files. No PyAV, libav bindings, or
other compiled dependencies are required.

## Usage

```bash
python -m cutkit probe --keyframes fixtures/source.mp4

python -m cutkit smart-cut \
  --input fixtures/source.mp4 \
  --start 4/3 \
  --duration 5/2 \
  --output /tmp/cutkit-out.mp4 \
  --qc /tmp/cutkit-qc.json \
  --subtitles fixtures/source.srt
```

`--subtitles` is optional; without it, the input's embedded subtitle stream is
used when present. `fixtures/source.mp4` is a checked-in 5 second test clip. See
`fixtures/README.md` for its layout (frame rate, GOP, audio tracks, subtitles).

## How smart-cut works

Times accept fractions (`4/3`), decimals (`1.333`), or clocks
(`00:00:01.333`) and stay exact as `Fraction` values. Frame boundaries are
computed from integer packet PTS plus the stream time base, so the first output
frame is exactly the frame presented at `--start`.

1. The video window is split into segments against the source keyframes.
   Complete in-window GOPs are rendered with `ffmpeg -c:v copy`; the partial
   GOP touching each window edge is re-encoded with `libx264` using the
   `trim=start_frame/end_frame` filter for frame accuracy.
2. Each segment is written as MPEG-TS (no edit list, exact frame durations)
   and joined with the concat demuxer via bitstream copy, producing continuous
   PTS with no gaps or black frames and no reference frames before the cut.
3. Every audio stream is trimmed to the same source window with `atrim` and
   encoded once to AAC. Because each track is cut at the same source instant,
   delays baked into the PCM (such as the fixture's 100 ms second-track delay)
   are preserved.
4. Subtitles are loaded from `--subtitles` when given, otherwise from the
   input's embedded subtitle stream. Cues are shifted by `-start`, dropped when
   fully outside the window, and clipped when they cross a boundary. They are
   muxed as `mov_text`; if the container rejects them, a same-name `.srt`
   sidecar is written next to the output.
5. A QC report is written to `--qc` with requested/actual durations, frame
   count, per-audio timing, the head/copy/tail source windows, subtitle
   counts, and an A/V sync estimate.

## Layout

- `cutkit/probe.py` — ffprobe wrappers (`container`, `packets`, `frames`, `keyframe_pts`)
- `cutkit/timecode.py` — clock / decimal / rational time parsing
- `cutkit/ffmpeg.py` — subprocess wrapper for the system ffmpeg binary
- `cutkit/plan.py` — frame-accurate window and GOP render-segment planning
- `cutkit/subtitles.py` — SRT/ASS loading, window clipping, SRT writing
- `cutkit/smartcut.py` — segment rendering, concat, audio/subtitle handling, QC
- `cutkit/errors.py` — user-facing exception types
- `cutkit/__main__.py` — `probe` and `smart-cut` CLI
- `fixtures/` — source media used for local work
- `scripts/generate_fixtures.sh` — regenerates the fixture from lavfi sources (should be bit-identical given the same ffmpeg build)
- `tests/` — unit tests for planning/timecode/subtitles and end-to-end cuts
