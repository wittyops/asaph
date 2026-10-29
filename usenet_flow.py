"""Usenet acquisition trigger + staging-import validation + supersede.
Reuses the existing Lidarr/Prowlarr/SABnzbd stack — this module adds a
trigger and a validation gate around it, it does not reimplement
acquisition (spec C4). Lidarr's root folder for this flow must point at a
staging path, never directly at music/library/ (spec §5.4)."""
from __future__ import annotations

import shutil
from pathlib import Path

import requests

from lib.validator import decode_check

AUDIO_EXTENSIONS = {".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".aac"}
HTTP_TIMEOUT_S = 15


def trigger_search(artist: str, album: str, lidarr_base: str, api_key: str) -> int:
    """Triggers Lidarr's AlbumSearch command for the named artist/album.
    Returns Lidarr's command ID for polling via GET /api/v1/command/<id>."""
    resp = requests.post(
        f"{lidarr_base}/api/v1/command",
        headers={"X-Api-Key": api_key},
        json={"name": "AlbumSearch", "artistName": artist, "albumTitle": album},
        timeout=HTTP_TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def check_completeness(staging_dir: Path, expected_track_count: int) -> bool:
    """True only if staging_dir contains exactly expected_track_count audio
    files and every one of them passes decode_check. A partial release
    (e.g. disc 1 of 2 posted) must fail this, not pass because Lidarr
    reported 'imported.'"""
    tracks = [
        p for p in staging_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
    ]
    if len(tracks) != expected_track_count:
        return False
    for track in tracks:
        if decode_check(track) is not None:
            return False
    return True


def supersede(old_dir: Path, new_dir: Path, archive_root: Path) -> None:
    """Archives old_dir under archive_root (reversible — never a hard
    delete), then moves new_dir to take old_dir's former place."""
    archive_dest = archive_root / old_dir.relative_to(old_dir.anchor)
    archive_dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(old_dir), str(archive_dest))
    old_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(new_dir), str(old_dir))
