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

def _make_tone_flac(tmp_path, name, af=None) -> Path:
    out = tmp_path / name
    cmd = ["ffmpeg", "-f", "lavfi", "-i", "sine=f=440:r=44100:d=10"]
    if af:
        cmd += ["-af", af]
    subprocess.run(cmd + ["-c:a", "flac", "-y", str(out)], check=True, capture_output=True)
    return out

def _make_truncated_file(tmp_path) -> Path:
    # 10 s tone is ~128 KB; keep the first 32 KB so the file clears the
    # 1024-byte size gate but the FLAC stream is cut mid-frame, so ffmpeg
    # itself must report the corruption.
    good = _make_tone_flac(tmp_path, "good.flac")
    out = tmp_path / "truncated.flac"
    out.write_bytes(good.read_bytes()[:32768])
    return out

def test_decode_check_passes_on_valid_file(tmp_path):
    f = _make_silent_flac(tmp_path)
    assert decode_check(f) is None

def test_decode_check_fails_on_truncated_file(tmp_path):
    f = _make_truncated_file(tmp_path)
    err = decode_check(f)
    assert err is not None
    assert isinstance(err, str)
    # Must come from the ffmpeg branch, not the size gate.
    assert err.startswith("ffmpeg decode"), err
    assert "too small" not in err

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

def test_signal_heuristic_ignores_normal_peak_containing_0_0_digits(tmp_path):
    # Peak around -20 dBFS: the string "0.0" appears in "-20.0x", but this is
    # not clipping and must not be flagged.
    f = _make_tone_flac(tmp_path, "quiet.flac", af="volume=0.8")
    assert signal_heuristic_check(f) == []

def test_signal_heuristic_flags_clipped_audio(tmp_path):
    f = _make_tone_flac(tmp_path, "clipped.flac", af="volume=40,aformat=sample_fmts=s16")
    suspects = signal_heuristic_check(f)
    assert suspects, "hard-clipped tone should produce at least one suspect"
    assert all(s["kind"] == "clipping" for s in suspects)

def test_checksum_verify_returns_none_without_sidecar(tmp_path):
    f = _make_silent_flac(tmp_path)
    assert checksum_verify(f, sfv_path=None) is None
