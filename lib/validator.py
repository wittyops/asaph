"""Shared audio validator — decode check, signal heuristic, checksum verify.

Used by three callers: ingress-sweeper (move mode), the library health scan
(read-only), and the usenet-import-validator. One implementation, not three.
No network calls here — this module is pure local file analysis.
"""
from __future__ import annotations

import subprocess
import zlib
from pathlib import Path

MIN_FILE_BYTES = 1024
DECODE_TIMEOUT_S = 60


def decode_check(path: Path) -> str | None:
    """Null-decode the file end to end. Returns an error string on failure,
    None if it decodes cleanly. Catches non-playable/truncated/container-
    corrupt files — the strongest, least-ambiguous signal."""
    try:
        if path.stat().st_size < MIN_FILE_BYTES:
            return f"file too small ({path.stat().st_size}B), likely incomplete"
        result = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
            capture_output=True, text=True, timeout=DECODE_TIMEOUT_S,
        )
        if result.returncode != 0:
            return f"ffmpeg decode error: {result.stderr.strip()[:300]}"
        if result.stderr.strip():
            # ffmpeg can exit 0 but still log stream errors (e.g. corrupt frames)
            return f"ffmpeg decode warning: {result.stderr.strip()[:300]}"
        return None
    except subprocess.TimeoutExpired:
        return f"decode timeout (>{DECODE_TIMEOUT_S}s)"
    except Exception as e:
        return f"decode check error: {e}"


def signal_heuristic_check(path: Path) -> list[dict]:
    """Flag suspect ranges (clipping, sample discontinuities) via ffmpeg's
    astats filter. Heuristic — returns suspects for human review, never a
    pass/fail verdict on its own."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-v", "info", "-i", str(path),
             "-af", "astats=metadata=1:reset=1",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=DECODE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return []
    except Exception:
        return []

    suspects: list[dict] = []
    current_time = 0.0
    for line in result.stderr.splitlines():
        line = line.strip()
        if line.startswith("[Parsed_astats") and "pts_time:" in line:
            try:
                current_time = float(line.split("pts_time:")[1].split()[0])
            except (IndexError, ValueError):
                pass
        elif "Peak level dB" in line and "0.0" in line:
            # Sample(s) pinned at 0dBFS — hard clipping
            suspects.append({
                "start_s": max(0.0, current_time - 0.5),
                "end_s": current_time + 0.5,
                "kind": "clipping",
            })
    return suspects


def checksum_verify(path: Path, sfv_path: Path | None) -> bool | None:
    """Verify path's CRC32 against an SFV sidecar, when one exists. Returns
    None (not False) when there's nothing to verify against — absence of an
    SFV is normal for YouTube-sourced ingress, not a failure."""
    if sfv_path is None or not sfv_path.exists():
        return None
    try:
        expected = None
        for line in sfv_path.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith(";"):
                continue
            parts = line.rsplit(None, 1)
            if len(parts) == 2 and parts[0].strip() == path.name:
                expected = parts[1].strip().lower()
                break
        if expected is None:
            return None
        crc = 0
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                crc = zlib.crc32(chunk, crc)
        return f"{crc & 0xFFFFFFFF:08x}" == expected
    except Exception:
        return None
