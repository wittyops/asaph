# usenet_poll.py
"""Polls Lidarr for completion of searches triggered via the review UI's
"Search usenet" action, and resolves the corresponding review_items row.

Closes the gap between search_usenet() (fires the Lidarr search, sets
status='searching') and usenet_flow.py's check_completeness()/supersede()
(built, reviewed, never called by anything until this module exists).

Safety posture: every ambiguous case leaves the row in 'searching' rather
than guessing. A stuck-but-safe row is an acceptable outcome; a
wrongly-resolved one is not -- supersede() moves files, and getting this
wrong on bad information means demoting a possibly-fine library file.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import requests

from usenet_flow import check_completeness, supersede

HTTP_TIMEOUT_S = 15


def _lidarr_queue(lidarr_base: str, api_key: str) -> list[dict]:
    resp = requests.get(
        f"{lidarr_base}/api/v1/queue",
        headers={"X-Api-Key": api_key},
        timeout=HTTP_TIMEOUT_S,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("records", data) if isinstance(data, dict) else data


def _queue_has_match(queue: list[dict], artist: str, album: str) -> bool:
    artist_l, album_l = artist.lower(), album.lower()
    for item in queue:
        item_artist = (item.get("artist", {}) or {}).get("artistName", "")
        item_album = (item.get("album", {}) or {}).get("title", "")
        if item_artist.lower() == artist_l and item_album.lower() == album_l:
            return True
    return False


def _lidarr_album_track_count(lidarr_base: str, api_key: str, artist: str, album: str) -> int | None:
    """Looks up the artist by name, then finds the matching album's
    statistics.trackCount. Returns None if the artist/album can't be
    found unambiguously -- callers must treat None as 'leave it alone',
    never as zero."""
    try:
        artists_resp = requests.get(
            f"{lidarr_base}/api/v1/artist",
            headers={"X-Api-Key": api_key},
            timeout=HTTP_TIMEOUT_S,
        )
        artists_resp.raise_for_status()
        matches = [a for a in artists_resp.json() if a.get("artistName", "").lower() == artist.lower()]
        if len(matches) != 1:
            return None
        artist_id = matches[0]["id"]

        albums_resp = requests.get(
            f"{lidarr_base}/api/v1/album",
            headers={"X-Api-Key": api_key},
            params={"artistId": artist_id},
            timeout=HTTP_TIMEOUT_S,
        )
        albums_resp.raise_for_status()
        album_matches = [a for a in albums_resp.json() if a.get("title", "").lower() == album.lower()]
        if len(album_matches) != 1:
            return None
        return album_matches[0].get("statistics", {}).get("trackCount")
    except Exception:
        return None


def poll_searching_items(
    db_path: Path,
    lidarr_base: str,
    api_key: str,
    staging_root: Path,
    library_root: Path,
    archive_root: Path,
) -> dict:
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT id, path, artist, album FROM review_items WHERE status='searching'"
    ).fetchall()

    summary = {"resolved": 0, "still_searching": 0, "errors": 0}

    for item_id, path, artist, album in rows:
        if not artist or not album:
            summary["still_searching"] += 1
            continue
        try:
            queue = _lidarr_queue(lidarr_base, api_key)
        except Exception:
            summary["errors"] += 1
            continue

        if _queue_has_match(queue, artist, album):
            summary["still_searching"] += 1
            continue

        staging_dir = staging_root / artist / album
        if not staging_dir.is_dir():
            summary["still_searching"] += 1
            continue

        expected_count = _lidarr_album_track_count(lidarr_base, api_key, artist, album)
        if expected_count is None:
            summary["still_searching"] += 1
            continue

        old_dir = library_root / artist / album
        now = datetime.now(timezone.utc).isoformat()

        if check_completeness(staging_dir, expected_count):
            if old_dir.exists():
                supersede(old_dir, staging_dir, archive_root)
            else:
                old_dir.parent.mkdir(parents=True, exist_ok=True)
                staging_dir.rename(old_dir)
            conn.execute(
                "UPDATE review_items SET status='resolved', resolved_at=?, "
                "reason=reason || ' [usenet grab complete, superseded old library files]' "
                "WHERE id=?",
                (now, item_id),
            )
            summary["resolved"] += 1
        else:
            conn.execute(
                "UPDATE review_items SET status='resolved', resolved_at=?, "
                "reason=reason || ' [usenet grab incomplete/partial -- left existing library file in place]' "
                "WHERE id=?",
                (now, item_id),
            )
            summary["resolved"] += 1

    conn.commit()
    conn.close()
    return summary
