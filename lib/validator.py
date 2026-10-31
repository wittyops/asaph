"""Shared audio validator — decode check, signal heuristic, checksum verify,
metadata check. Used by four callers: ingress-sweeper (move mode), the
library health scan (read-only), curator.py's own periodic scan, and
usenet_poll.py's post-promotion check (added 2026-10-31 -- usenet-acquired
files were being promoted into the library on integrity/completeness alone,
never checked for missing tags or album art the way ingress-sweeper-routed
files are). One implementation, not four. No network calls here -- this
module is pure local file analysis.
"""
from __future__ import annotations

import re
import subprocess
import zlib
from pathlib import Path

import mutagen

MIN_FILE_BYTES = 1024
DECODE_TIMEOUT_S = 60
CLIP_PEAK_DB = -0.1
PEAK_LEVEL_RE = re.compile(r"Peak level dB:\s*(\S+)")


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
        elif m := PEAK_LEVEL_RE.search(line):
            try:
                peak_db = float(m.group(1))
            except ValueError:
                continue
            if peak_db < CLIP_PEAK_DB:
                continue
            # Sample(s) at or within 0.1 dB of 0dBFS — hard clipping
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


def check_metadata(path: Path) -> list[str]:
    """Returns a list of metadata issue strings, empty when clean. Checks
    title/artist/album tags and embedded (or folder) album art -- the same
    gaps ingress-sweeper-routed files get checked for, now also run against
    usenet-acquired files before/after promotion (see usenet_poll.py)."""
    issues = []
    try:
        audio = mutagen.File(str(path), easy=True)
        if audio is None:
            return ["mutagen: unrecognized format"]
        for tag in ["title", "artist", "album"]:
            val = audio.get(tag, [])
            if not val or not str(val[0]).strip():
                issues.append(f"missing {tag}")

        # Album art: check embedded, then folder
        audio_raw = mutagen.File(str(path))
        has_art = False
        if audio_raw:
            if hasattr(audio_raw, "pictures"):
                # FLAC/OGG expose art via .pictures, never via ID3 APIC
                # frames -- decide definitively here and never fall through
                # to the APIC-tag branch below, which assumes .tags holds
                # (key, value) string pairs. For FLAC specifically, .tags
                # is a VComment -- a list of (key, value) TUPLES, not a
                # dict of string keys -- so "k.startswith(...)" in that
                # branch raised AttributeError on every FLAC with any
                # Vorbis comment set and no embedded picture (i.e. most
                # real FLAC files), silently misreported as a generic
                # "metadata error" instead of the real "no album art"
                # finding. Confirmed live 2026-10-31 while wiring this
                # into usenet_poll.py's post-promotion check.
                has_art = bool(audio_raw.pictures)
            elif hasattr(audio_raw, "tags") and audio_raw.tags:
                has_art = any(str(k).startswith("APIC") for k in audio_raw.tags)
            elif "covr" in (audio_raw or {}):
                has_art = True

        if not has_art:
            album_dir = path.parent
            folder_art_names = ["albumart", "cover", "folder", "front", "AlbumArt"]
            has_folder_art = any(
                list(album_dir.glob(f"{name}.*"))
                for name in folder_art_names
            )
            if not has_folder_art:
                issues.append("no album art (embedded or folder)")
    except Exception as e:
        issues.append(f"metadata error: {e}")
    return issues
