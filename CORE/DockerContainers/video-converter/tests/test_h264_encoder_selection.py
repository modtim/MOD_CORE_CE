"""Unit tests for the H.264 encoder selection in convert.py (FIX-CE-H264-CONVERT-PY).

Owner decision (owner 11-11): CE gets H.264 by ROUTE B = Cisco libopenh264 INSIDE
the existing LGPL ffmpeg builds (ffmpeg stays --disable-gpl --disable-nonfree;
NO x264/GPL). The old probe (convert.py:53) returned a bare yes/no for the
regex libx264|libopenh264|h264_, while convert.py:84 hard-coded `-c:v libx264
-preset fast`: on a libopenh264 or hardware build the probe said yes and the
command died "Unknown encoder 'libx264'" -> CONVERSION_FAILURE.

The fix returns the CHOSEN encoder name (preference libx264 > libopenh264 >
h264_nvenc/h264_vaapi) and builds args per encoder. libx264 args stay byte-for-
byte; libopenh264 is constrained-baseline and rejects -preset ("Unrecognized
option 'preset'"), -crf, -tune, -profile:v and -x264-params.

No container and no real ffmpeg here: `ffmpeg -encoders` output is faked by
monkeypatching subprocess.run, and the resulting argv is asserted exactly.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import convert as vc  # noqa: E402

IN = "/data/input/clip.mkv"
MP4 = "/data/output/clip.mp4"
MOV = "/data/output/clip.mov"

TABLE_X264 = """FFmpeg version 9.0.2
 V..... libx264              libx264 H.264 (AAC/H.264) encoder
 V..... libvpx               libvpx VP8
 A..... aac                Audio codec
"""
TABLE_OPEN = """FFmpeg version 9.0.2
 V..... libopenh264          libopenh264 H.264 encoder
 V..... libvpx               libvpx VP8
 A..... aac                Audio codec
"""
TABLE_BOTH = """FFmpeg version 9.0.2
 V..... libx264              libx264 H.264 (AAC/H.264) encoder
 V..... libopenh264          libopenh264 H.264 encoder
 A..... aac                Audio codec
"""
TABLE_NVENC = """FFmpeg version 9.0.2
 V..... h264_nvenc           NVIDIA libavcodec H.264 encoder
 V..... libvpx               libvpx VP8
 A..... aac                Audio codec
"""
TABLE_NONE = """FFmpeg version 9.0.2
 V..... libvpx               libvpx VP8
 V..... libvpx-vp9           libvpx VP9
 A..... aac                Audio codec
"""


class FakeRun:
    """Stands in for subprocess.run: answers `ffmpeg -encoders` with a table."""

    def __init__(self, table):
        self.table = table

    def run(self, *args, **kwargs):
        class R:
            returncode = 0
            stdout = self.table
        return R()


def _argv(target, table):
    """Run convert.py's argv build for `target` with faked `ffmpeg -encoders`."""
    real = subprocess.run
    vc.subprocess.run = FakeRun(table).run
    try:
        return vc.build_cmd(Path(IN), target, Path(MP4 if target == "mp4" else MOV))
    finally:
        vc.subprocess.run = real


def test_libx264_only_keeps_paid_args_byte_for_byte():
    cmd = _argv("mp4", TABLE_X264)
    assert cmd == ["ffmpeg", "-y", "-i", IN,
                   "-c:v", "libx264", "-c:a", "aac", "-preset", "fast", MP4], cmd


def test_libopenh264_only_named_and_no_preset():
    cmd = _argv("mp4", TABLE_OPEN)
    assert cmd == ["ffmpeg", "-y", "-i", IN,
                   "-c:v", "libopenh264", "-c:a", "aac", "-strict", "-2", MP4], cmd
    assert "-preset" not in cmd and "preset" not in cmd


def test_both_encoders_libx264_wins():
    cmd = _argv("mp4", TABLE_BOTH)
    assert cmd == ["ffmpeg", "-y", "-i", IN,
                   "-c:v", "libx264", "-c:a", "aac", "-preset", "fast", MP4], cmd


def test_h264_nvenc_only_gets_its_own_args():
    cmd = _argv("mp4", TABLE_NVENC)
    assert cmd == ["ffmpeg", "-y", "-i", IN,
                   "-c:v", "h264_nvenc", "-c:a", "aac", "-strict", "-2", MP4], cmd
    assert "-preset" not in cmd


def test_none_gives_vp9_aac_fallback_exactly_as_today():
    cmd = _argv("mp4", TABLE_NONE)
    assert cmd == ["ffmpeg", "-y", "-i", IN,
                   "-c:v", "libvpx-vp9", "-c:a", "aac", "-strict", "-2", MP4], cmd


def test_mov_adds_f_mp4_on_every_vp9_path():
    cmd = _argv("mov", TABLE_NONE)
    assert cmd == ["ffmpeg", "-y", "-i", IN,
                   "-c:v", "libvpx-vp9", "-c:a", "aac", "-strict", "-2",
                   "-f", "mp4", MOV], cmd


def test_mov_needs_no_f_mp4_when_h264_exists():
    # Unchanged from the old paid path: H.264 muxes into mov, so the container
    # stays mov (byte-for-byte with convert.py:83-85 today).
    for table in (TABLE_X264, TABLE_OPEN, TABLE_NVENC):
        cmd = _argv("mov", table)
        assert "-f" not in cmd, cmd


def test_webm_target_unchanged():
    cmd = _argv("webm", TABLE_BOTH)
    assert cmd == ["ffmpeg", "-y", "-i", IN,
                   "-c:v", "libvpx-vp9", "-c:a", "libopus", MOV], cmd


def test_probe_returns_encoder_name_not_bool():
    assert vc.pick_h264_encoder.__name__ == "pick_h264_encoder"
    for table, want in ((TABLE_X264, "libx264"), (TABLE_OPEN, "libopenh264"),
                        (TABLE_BOTH, "libx264"), (TABLE_NVENC, "h264_nvenc"),
                        (TABLE_NONE, None)):
        vc.subprocess.run = FakeRun(table).run
        try:
            assert vc.pick_h264_encoder() == want, (table, want)
        finally:
            vc.subprocess.run = subprocess.run
