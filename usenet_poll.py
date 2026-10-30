# usenet_poll.py
"""Polls SABnzbd for completion of grabs triggered via the review UI's
"Search usenet" action, and resolves the corresponding review_items row.

Closes the gap between search_usenet() (searches Prowlarr, grabs via
SABnzbd, sets status='searching' with the resulting nzo_id) and
usenet_flow.py's check_completeness()/supersede() (built, reviewed,
never called by anything until this module exists).

Correlation is by the `nzo_id` column on review_items, not by
artist/album name-matching against anything -- SABnzbd's queue/history
are keyed by nzo_id, so that's the one reliable join key.

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

from lib.resolver import musicbrainz_track_count
from usenet_flow import check_completeness, remap_sabnzbd_path, rename_into_staging_layout, supersede

HTTP_TIMEOUT_S = 15


def _sabnzbd_queue(sabnzbd_base: str, api_key: str) -> list[dict]:
    resp = requests.get(
        f"{sabnzbd_base}/api",
        params={"mode": "queue", "apikey": api_key, "output": "json"},
        timeout=HTTP_TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp.json().get("queue", {}).get("slots", [])


def _sabnzbd_history(sabnzbd_base: str, api_key: str) -> list[dict]:
    resp = requests.get(
        f"{sabnzbd_base}/api",
        params={"mode": "history", "apikey": api_key, "output": "json", "limit": 50},
        timeout=HTTP_TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp.json().get("history", {}).get("slots", [])


def _mark_incomplete(conn, item_id: int, now: str, summary: dict) -> None:
    """Shared resolution path for 'grab incomplete/partial' -- no files
    are moved in this branch, so a failed UPDATE is always safe to retry
    next cycle."""
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
        return
    summary["resolved"] += 1


def poll_searching_items(
    db_path: Path,
    sabnzbd_base: str,
    api_key: str,
    staging_root: Path,
    library_root: Path,
    archive_root: Path,
    sabnzbd_staging_prefix: Path,
) -> dict:
    # timeout=30: a busy-wait tolerance so a brief write from the review UI
    # or a concurrent poll/scan doesn't immediately raise "database is
    # locked" -- see health_scan.py's matching change (Fix 4).
    conn = sqlite3.connect(str(db_path), timeout=30)
    rows = conn.execute(
        "SELECT id, path, artist, album, nzo_id FROM review_items WHERE status='searching'"
    ).fetchall()

    summary = {"resolved": 0, "still_searching": 0, "errors": 0}

    for item_id, path, artist, album, nzo_id in rows:
        if not artist or not album or not nzo_id:
            summary["still_searching"] += 1
            continue

        try:
            queue = _sabnzbd_queue(sabnzbd_base, api_key)
        except Exception:
            summary["errors"] += 1
            continue

        if any(slot.get("nzo_id") == nzo_id for slot in queue):
            summary["still_searching"] += 1
            continue

        try:
            history = _sabnzbd_history(sabnzbd_base, api_key)
        except Exception:
            summary["errors"] += 1
            continue

        history_slot = next((slot for slot in history if slot.get("nzo_id") == nzo_id), None)
        if history_slot is None:
            # Neither queue nor history has this nzo_id -- don't know yet.
            # Never guess; leave it in 'searching'.
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

        if history_slot.get("status") != "Completed":
            # Failed (or any other terminal non-Completed status) -- same
            # outcome as an incomplete/partial grab: mark resolved, leave
            # the old library file in place.
            _mark_incomplete(conn, item_id, now, summary)
            continue

        # Resolve expected_count BEFORE touching the SABnzbd-side folder at
        # all. rename_into_staging_layout() empties/removes that source
        # folder -- if we moved it first and THEN found the track-count
        # lookup ambiguous/failing, the next poll cycle would recompute the
        # same remapped path from SABnzbd's history and call
        # rename_into_staging_layout() on a directory that no longer
        # exists (FileNotFoundError), permanently stranding the row even
        # though its files are actually sitting safely in staging already.
        # Leaving the source untouched this cycle keeps retry safe.
        try:
            expected_count = musicbrainz_track_count(artist, album)
        except Exception:
            summary["errors"] += 1
            continue
        if expected_count is None:
            summary["still_searching"] += 1
            continue

        storage = history_slot.get("storage")
        try:
            remapped = remap_sabnzbd_path(storage, sabnzbd_staging_prefix, staging_root)
        except Exception:
            # storage doesn't start with the configured prefix -- category
            # config drifted. Treat as an error outcome, don't crash the
            # whole poll cycle, and never guess a path.
            summary["errors"] += 1
            continue

        try:
            staging_dir = rename_into_staging_layout(remapped, artist, album, staging_root)
        except Exception:
            summary["errors"] += 1
            continue

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
            _mark_incomplete(conn, item_id, now, summary)

    conn.close()
    return summary
