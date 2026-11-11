#!/usr/bin/env bash
# Regenerate fixtures/source.mp4 from lavfi sources. Keep flags in sync with fixtures/README.md.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/fixtures/source.mp4"
SRT="$ROOT/fixtures/source.srt"

ffmpeg -y -hide_banner -loglevel error \
  -f lavfi -i "testsrc2=size=320x240:rate=24:duration=5,format=yuv420p" \
  -f lavfi -i "sine=frequency=440:sample_rate=48000:duration=5" \
  -f lavfi -i "sine=frequency=880:sample_rate=48000:duration=5" \
  -i "$SRT" \
  -filter_complex "[1:a]aformat=sample_fmts=fltp:channel_layouts=mono,asetpts=PTS-STARTPTS[a0];[2:a]adelay=delays=100:all=1,aformat=sample_fmts=fltp:channel_layouts=mono,asetpts=PTS-STARTPTS[a1]" \
  -map 0:v -map "[a0]" -map "[a1]" -map 3:s \
  -c:v libx264 -preset veryfast -crf 23 -pix_fmt yuv420p \
  -g 12 -keyint_min 12 -sc_threshold 0 -bf 0 -x264-params "open-gop=0" \
  -c:a aac -ar 48000 -ac 1 -b:a 64k \
  -c:s mov_text \
  -movflags +faststart \
  -metadata:s:s:0 language=eng \
  "$OUT"

echo "wrote $OUT"
ffprobe -hide_banner -show_entries format=duration -show_entries stream=index,codec_type,codec_name,nb_frames,duration,start_time -of compact "$OUT"
