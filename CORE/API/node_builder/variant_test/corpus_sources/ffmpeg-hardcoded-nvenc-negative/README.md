# ffmpeg-hardcoded-nvenc-negative (corpus synthetic, manifest entry 16)

KNOWN-NEGATIVE entry. `main.sh` re-encodes the first input video with the
encoder HARD-CODED to `h264_nvenc` (NVIDIA NVENC). There is deliberately NO
runtime encoder detection and NO fallback — the image builds fine on every
accelerator (ffmpeg is present), but the run MUST fail non-zero on every
non-NVIDIA accelerator and record `incompat` there. The only expected pass
is cuda. Do not "fix" the encoder fallback; the negative verdict is the
point of this corpus entry.

I/O: first file in `/data/input` -> `/data/output/output.mp4` (video
comparer; only reached on cuda).
