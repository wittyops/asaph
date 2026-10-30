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
    never as zero.

    A genuine API failure (connection error, timeout, non-2xx response) is
    NOT caught here -- it propagates to the caller so it can be counted as
    an 'errors' outcome rather than folded into the same bucket as a
    legitimate not-found/ambiguous lookup. Only the ambiguous-match cases
    return None."""
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


def poll_searching_items(
    db_path: Path,
    lidarr_base: str,
    api_key: str,
    staging_root: Path,
    library_root: Path,
    archive_root: Path,
) -> dict:
    # timeout=30: a busy-wait tolerance so a brief write from the review UI
    # or a concurrent poll/scan doesn't immediately raise "database is
    # locked" -- see health_scan.py's matching change (Fix 4).
    conn = sqlite3.connect(str(db_path), timeout=30)
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

        try:
            expected_count = _lidarr_album_track_count(lidarr_base, api_key, artist, album)
        except Exception:
            summary["errors"] += 1
            continue
        if expected_count is None:
            summary["still_searching"] += 1
            continue

        # old_dir comes from the row's own real file path, NOT a fresh
        # artist/album reconstruction -- the library's real directory names
        # go through ingress_sweeper.py's sanitize() (colon substitution,
        # unsafe-char stripping, etc.), so artist/album strings from the
        # review form will not reliably match the actual on-disk directory.
        # The row's `path` column already points at the real file under the
        # real directory, so its parent is the one safe source of truth.
        old_dir = Path(path).parent
        now = datetime.now(timezone.utc).isoformat()

        if check_completeness(staging_dir, expected_count):
            if old_dir.exists():
                supersede(old_dir, staging_dir, archive_root)
            else:
                old_dir.parent.mkdir(parents=True, exist_ok=True)
                staging_dir.rename(old_dir)
            try:
                conn.execute(
                    "UPDATE review_items SET status='resolved', resolved_at=?, "
                    "reason=reason || ' [usenet grab complete, superseded old library files]' "
                    "WHERE id=?",
                    (now, item_id),
                )
                conn.commit()
            except Exception as e:
                # Files have already been moved by check_completeness()'s
                # True branch above -- that's done and can't be silently
                # undone. If the row update itself fails (e.g. a lock that
                # outlasts the busy timeout), surface it as an error rather
                # than losing track of the fact that this row's on-disk
                # state no longer matches its DB status, and don't let it
                # abort rows already committed earlier in this same cycle.
                summary["errors"] += 1
                print(f"usenet_poll: row {item_id} UPDATE failed after files were "
                      f"already moved (dir now in sync, DB status stale): {e}")
                continue
            summary["resolved"] += 1
        else:
            try:
                conn.execute(
                    "UPDATE review_items SET status='resolved', resolved_at=?, "
                    "reason=reason || ' [usenet grab incomplete/partial -- left existing library file in place]' "
                    "WHERE id=?",
                    (now, item_id),
                )
                conn.commit()
            except Exception as e:
                summary["errors"] += 1
                print(f"usenet_poll: row {item_id} UPDATE failed (no files moved "
                      f"for this row, safe to retry next cycle): {e}")
                continue
            summary["resolved"] += 1

    conn.close()
    return summary
