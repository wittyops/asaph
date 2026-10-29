import subprocess
from pathlib import Path
import pytest
from validator import decode_check, signal_heuristic_check, checksum_verify

def _make_silent_flac(tmp_path, seconds=2) -> Path:
    out = tmp_path / "silent.flac"
    subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono",
         "-t", str(seconds), "-y", str(out)],
        check=True, capture_output=True,
    )
    return out

def _make_truncated_file(tmp_path) -> Path:
    good = _make_silent_flac(tmp_path)
    out = tmp_path / "truncated.flac"
    out.write_bytes(good.read_bytes()[:200])  # cut off mid-stream
    return out

def test_decode_check_passes_on_valid_file(tmp_path):
    f = _make_silent_flac(tmp_path)
    assert decode_check(f) is None

def test_decode_check_fails_on_truncated_file(tmp_path):
    f = _make_truncated_file(tmp_path)
    err = decode_check(f)
    assert err is not None
    assert isinstance(err, str)

def test_decode_check_fails_on_tiny_file(tmp_path):
    f = tmp_path / "empty.flac"
    f.write_bytes(b"\x00" * 10)
    err = decode_check(f)
    assert err is not None
    assert "small" in err.lower() or "decode" in err.lower()

def test_signal_heuristic_returns_empty_on_clean_silence(tmp_path):
    f = _make_silent_flac(tmp_path)
    suspects = signal_heuristic_check(f)
    assert suspects == []

def test_checksum_verify_returns_none_without_sidecar(tmp_path):
    f = _make_silent_flac(tmp_path)
    assert checksum_verify(f, sfv_path=None) is None
