# cutkit

A small media toolkit built around system `ffmpeg` / `ffprobe`.

- `probe` prints container, stream and keyframe information.
- `smart-cut` performs a GOP-aware, frame-accurate window cut: complete GOPs
  inside the window are bitstream-copied, and only the partial GOPs touching
  the window edges are re-encoded. Audio tracks and subtitles are aligned to
  the same window and a QC report is written next to the output.

## Requirements

- Python 3.11+ (stdlib only)
- `ffmpeg` and `ffprobe` on `PATH`

No containers are used. Do not add Docker files.

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

`--start` / `--duration` accept fractions (`4/3`), decimals (`1.3333`) and
clock time (`00:00:01.333`). The window is half-open: frames with
`start <= pts < start + duration` are kept, so the first output frame is the
source frame at `--start` with no extra lead-in.

### How smart-cut works

1. ffprobe packet data locates every frame and keyframe in source PTS order.
2. The window is split into a re-encoded head (partial leading GOP),
   bitstream-copied whole GOPs, and a re-encoded tail (partial trailing GOP).
3. Edge segments are decoded from the preceding keyframe and trimmed with the
   `trim` filter; whole GOPs are extracted with `-c copy`. Segments are raw
   H.264 and concatenated at one constant frame rate, so output timestamps are
   uniform (a 12288 Hz track timescale is used for MP4-family outputs).
4. Every audio stream is trimmed to the same window with `atrim` and re-encoded
   to AAC, preserving each stream's timing relative to the video and to each
   other.
5. External SRT/ASS subtitles (or the embedded subtitle stream) are shifted by
   `-start`, clipped to the window and re-timed from zero. Non-overlapping
   cues are muxed as `mov_text`; when cues overlap (which `mov_text` cannot
   represent) or the container rejects subtitles, a same-name `.srt` sidecar is
   written next to the video instead.
6. A QC JSON reports requested/actual durations and frame counts, the source
   time bounds of each rendered/copied segment, subtitle statistics and the
   measured A/V offset.

Errors (missing input, window past the media duration, missing ffmpeg/ffprobe,
unparseable times) exit non-zero with a message on stderr.

`fixtures/source.mp4` is a checked-in 5 second test clip. See `fixtures/README.md` for its layout (frame rate, GOP, audio tracks, subtitles).

## Layout

- `cutkit/probe.py` — ffprobe wrappers (`container`, `packets`, `frames`, `keyframe_pts`)
- `cutkit/timecode.py` — clock / decimal / rational time parsing
- `cutkit/ffmpeg.py` — system ffmpeg runner
- `cutkit/plan.py` — frame/GOP inspection and window segmentation
- `cutkit/subtitles.py` — SRT/ASS parsing, window clipping and re-timing
- `cutkit/smartcut.py` — end-to-end smart cut orchestration and QC
- `cutkit/__main__.py` — `probe` and `smart-cut` CLI
- `tests/` — pytest suite (plan boundaries, subtitles, end-to-end ffmpeg checks)
- `fixtures/` — source media used for local work
- `scripts/generate_fixtures.sh` — regenerates the fixture from lavfi sources (should be bit-identical given the same ffmpeg build)

## Tests

```bash
python -m pytest tests/
```

The end-to-end tests require `ffmpeg`/`ffprobe` on `PATH` and skip cleanly
otherwise. They verify the 60-frame reference cut, byte-identical VCL NALs in
the copied GOP region, first-frame identity, both audio tracks, subtitle edge
handling and QC report contents.
