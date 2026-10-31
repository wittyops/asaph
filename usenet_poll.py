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

from lib.resolver import musicbrainz_track_count
from lib.validator import check_metadata
from usenet_flow import (
    AUDIO_EXTENSIONS,
    check_completeness,
    remap_sabnzbd_path,
    rename_into_staging_layout,
    sabnzbd_history,
    sabnzbd_queue,
    supersede,
)


def _check_promoted_metadata(final_dir: Path) -> tuple[bool, str]:
    """Runs check_metadata() on every track at its final, post-promotion
    library path. check_completeness() already proved these files decode
    and the track count matches -- this is a SEPARATE check (tags, embedded
    art) that nothing in the usenet flow ran before this. Never blocks or
    reverses the promotion: the file is still the best available copy
    either way. Pure function, no DB access -- the caller folds the result
    into the same UPDATE that resolves (or doesn't resolve) the row, so
    there's one atomic write instead of two.

    Returns (clean, suffix). When clean is False, the caller should leave
    status='pending' on this SAME row rather than resolving it, so it flows
    back through the existing review queue UI -- relabel/art-select -- for
    a human fix. Originally this spawned a brand new review_items row for a
    gap, but the promoted file sits at the exact path review_items.path
    already has UNIQUE'd for this row -- INSERTing a second row at that
    path always violated the constraint and silently no-opped. Reusing the
    row instead sidesteps that entirely."""
    tracks = sorted(
        p for p in final_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
    )
    flagged = [(t, check_metadata(t)) for t in tracks]
    flagged = [(t, issues) for t, issues in flagged if issues]

    if not flagged:
        return True, " [metadata check: clean]"

    _, first_issues = flagged[0]
    summary_text = f"{len(flagged)}/{len(tracks)} track(s) have gaps: {', '.join(first_issues)}"
    if len(flagged) > 1:
        summary_text += " (+others)"
    return False, f" [metadata check: {summary_text} -- needs relabel/art fix]"


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
            queue = sabnzbd_queue(sabnzbd_base, api_key)
        except Exception:
            summary["errors"] += 1
            continue

        if any(slot.get("nzo_id") == nzo_id for slot in queue):
            summary["still_searching"] += 1
            continue

        try:
            history = sabnzbd_history(sabnzbd_base, api_key)
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

            # old_dir is now the final, real library path (supersede()/
            # rename() above just put the promoted files there) -- check
            # metadata there, not in staging, so a later relabel/art-select
            # fix targets the actual library file. A check failure here
            # (ffprobe missing, permissions, etc.) must not be mistaken for
            # a metadata GAP -- fall back to "clean" (don't block on an
            # unrelated tooling error) but still surface it as an error.
            try:
                metadata_clean, metadata_suffix = _check_promoted_metadata(old_dir)
            except Exception as e:
                summary["errors"] += 1
                print(f"usenet_poll: row {item_id} metadata check itself failed "
                      f"(promotion already succeeded, not affected): {e}")
                metadata_clean, metadata_suffix = True, " [metadata check: error, not run]"

            try:
                if metadata_clean:
                    conn.execute(
                        "UPDATE review_items SET status='resolved', resolved_at=?, "
                        "reason=reason || ? WHERE id=?",
                        (now, " [usenet grab complete, superseded old library files]" + metadata_suffix, item_id),
                    )
                else:
                    # status was 'searching' going into this cycle --
                    # must be set explicitly to 'pending' here, not left
                    # alone, or poll_searching_items's own WHERE
                    # status='searching' query would pick this row up
                    # again every cycle forever (the nzo_id/history lookup
                    # would still resolve it the same way each time, just
                    # endlessly re-running this same metadata check).
                    # Not 'resolved' though -- it needs a human fix, and
                    # 'pending' is what puts it back in the review queue.
                    conn.execute(
                        "UPDATE review_items SET status='pending', reason=reason || ? WHERE id=?",
                        (" [usenet grab complete, superseded old library files]" + metadata_suffix, item_id),
                    )
                conn.commit()
            except Exception as e:
                # Files have already been moved above -- that's done and
                # can't be silently undone. If the row update itself fails
                # (e.g. a lock that outlasts the busy timeout), surface it
                # as an error rather than losing track of the fact that
                # this row's on-disk state no longer matches its DB status,
                # and don't let it abort rows already committed earlier in
                # this same cycle.
                summary["errors"] += 1
                print(f"usenet_poll: row {item_id} UPDATE failed after files were "
                      f"already moved (dir now in sync, DB status stale): {e}")
                continue

            summary["resolved"] += 1
        else:
            _mark_incomplete(conn, item_id, now, summary)

    conn.close()
    return summary
