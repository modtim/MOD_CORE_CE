"""Tests for the .usdz container loader (generate_thumbnail._unwrap_usdz_package).

No DB, no MinIO, no API callback: the loader reads one already-downloaded temp
file and opens nothing else. Guard tests run everywhere; the mesh-level
integration test needs trimesh + pxr (skipped where the harness image lacks
them - a skipped leg is a reportable fact, never a fake pass).
"""
import os
import shutil
import stat
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import generate_thumbnail as gt  # noqa: E402

MB = 1024 * 1024

USDA = (
    "#usda 1.0\n(\n    defaultStage = true\n"
    "    def Mesh \"tri\" {\n"
    "        int[] faceVertexCounts = [3]\n"
    "        int[] faceVertexIndices = [0, 1, 2]\n"
    "        point3f points = [(0,0,0), (1,0,0), (0,1,0)]\n"
    "    }\n)\n"
)


def _mk_usdz(path, members):
    """members: (name, data) or (name, data, external_attr)."""
    with zipfile.ZipFile(path, 'w') as zf:
        for item in members:
            name, data = item[0], item[1]
            if len(item) > 2:
                zi = zipfile.ZipInfo(name)
                zi.external_attr = item[2]
                zi.compress_type = zipfile.ZIP_STORED
                zf.writestr(zi, data)
            else:
                zf.writestr(name, data)
    return str(path)


def test_zip_slip_member_rejected(tmp_path):
    p = _mk_usdz(tmp_path / 'slip.usdz', [('../evil.usda', USDA)])
    with pytest.raises(ValueError, match='traverses parent'):
        gt._unwrap_usdz_package(p)


def test_absolute_member_rejected(tmp_path):
    p = _mk_usdz(tmp_path / 'abs.usdz', [('/etc/evil.usda', USDA)])
    with pytest.raises(ValueError, match='absolute'):
        gt._unwrap_usdz_package(p)


def test_symlink_member_rejected(tmp_path):
    link_attr = (0o120000 << 16) | 0o777
    p = _mk_usdz(tmp_path / 'link.usdz', [('real.usda', USDA), ('l.usda', '/etc/x', link_attr)])
    with pytest.raises(ValueError, match='not a regular file'):
        gt._unwrap_usdz_package(p)


def test_entry_count_cap(tmp_path):
    members = [(f'm{i}.txt', 'x') for i in range(513)]
    p = _mk_usdz(tmp_path / 'many.usdz', members)
    with pytest.raises(ValueError, match='too many entries'):
        gt._unwrap_usdz_package(p)


def test_member_size_cap(tmp_path):
    # ZIP_LZMA (not DEFLATE): the harness image has no zlib, but lzma/bz2 are stdlib
    # and present (probed 11-08). Compressed member stays under the archive cap while
    # its declared uncompressed size crosses the per-member cap.
    with zipfile.ZipFile(tmp_path / 'big.usdz', 'w', zipfile.ZIP_LZMA) as zf:
        zf.writestr('a.usda', USDA)
        zf.writestr('b.bin', b'\0' * (64 * MB + 1))
    with pytest.raises(ValueError, match='uncompressed size too large'):
        gt._unwrap_usdz_package(str(tmp_path / 'big.usdz'))


def test_total_size_cap(tmp_path):
    # per-member ratio ~10 (< 32) and each member < 64 MiB, but total uncompressed
    # > 256 MiB while the archive itself stays under the 64 MiB compressed cap.
    blob = os.urandom(6 * MB) * 10  # 60 MiB uncompressed, ~6 MiB compressed
    with zipfile.ZipFile(tmp_path / 'tot.usdz', 'w', zipfile.ZIP_LZMA) as zf:
        zf.writestr('a.usda', USDA)
        for i in range(5):
            zf.writestr(f'b{i}.bin', blob)
    with pytest.raises(ValueError, match='total uncompressed size too large'):
        gt._unwrap_usdz_package(str(tmp_path / 'tot.usdz'))


def test_archive_compressed_size_cap(tmp_path):
    p = _mk_usdz(tmp_path / 'arch.usdz', [('a.usda', USDA), ('b.bin', os.urandom(64 * MB + 1))])
    with pytest.raises(ValueError, match='compressed size too large'):
        gt._unwrap_usdz_package(p)


def test_compression_ratio_cap(tmp_path):
    with zipfile.ZipFile(tmp_path / 'bomb.usdz', 'w', zipfile.ZIP_LZMA) as zf:
        zf.writestr('a.usda', USDA)
        zf.writestr('b.bin', b'\0' * (2 * MB))
    with pytest.raises(ValueError, match='compression ratio too high'):
        gt._unwrap_usdz_package(str(tmp_path / 'bomb.usdz'))


def test_malformed_zip_raises(tmp_path):
    (tmp_path / 'bad.usdz').write_bytes(b'PK\x03not-a-zip')
    with pytest.raises((ValueError, zipfile.BadZipFile)):
        gt._unwrap_usdz_package(str(tmp_path / 'bad.usdz'))


def test_no_usd_member_raises(tmp_path):
    p = _mk_usdz(tmp_path / 'noused.usdz', [('readme.txt', 'hi'), ('tex.png', b'\x89PNG')])
    with pytest.raises(ValueError, match='No USD file found'):
        gt._unwrap_usdz_package(p)


def test_nested_usda_wins_over_sibling_txt(tmp_path):
    p = _mk_usdz(tmp_path / 'pick.usdz', [
        ('notes.txt', 'hi'),
        ('geo/model.usda', USDA),
        ('tex.png', b'\x89PNG'),
    ])
    inner = gt._unwrap_usdz_package(p)
    try:
        assert Path(inner).name == 'model.usda'
        assert Path(inner).suffix == '.usda'
        assert Path(inner).exists()
    finally:
        shutil.rmtree(os.path.dirname(inner), ignore_errors=True)


def test_degenerate_dot_member_rejected_by_realpath_guard(tmp_path):
    # The name guards let the degenerate member '.' through; only the realpath
    # containment re-check rejects it (ValueError, not the IsADirectoryError the
    # open() raises without that guard).
    p = _mk_usdz(tmp_path / 'dot.usdz', [('.', 'y'), ('a.usda', USDA)])
    with pytest.raises(ValueError, match='resolves outside temp dir'):
        gt._unwrap_usdz_package(p)


def test_usdz_wrapping_mesh_loads_vertices():
    """Integration: usdz -> unwrap -> USD loader -> mesh with vertices."""
    pytest.importorskip('trimesh')
    pytest.importorskip('pxr')
    with tempfile.TemporaryDirectory() as td:
        p = _mk_usdz(Path(td) / 'tri.usdz', [('geo/tri.usda', USDA)])
        mesh = gt.load_mesh(p, 'usdz')
        assert len(mesh.vertices) == 3
        assert len(mesh.faces) == 1
