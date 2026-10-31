"""Polls Prowlarr for each watched artist/album and surfaces new releases as
pending candidates for one-click approval -- never grabs on its own.

Deliberately NOT a blind auto-grabber. Every other resolution path in this
codebase treats an unreviewed match as unsafe to act on (musicbrainz_
track_count()'s ambiguous-match handling, poll_searching_items()'s "never
guess" posture) -- a watchlist that silently grabbed the moment a plausible
release appeared would be the first place in Asaph that spends real
bandwidth/storage with no human ever looking at the match first. So this
module's only side effect is writing watchlist_candidates rows; the actual
grab happens in watchlist.py's approve route, when a human clicks it.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from usenet_flow import guess_artist_album, search_prowlarr


def poll_watchlist(
    db_path: Path, prowlarr_base: str, api_key: str,
    only_artist: str | None = None, only_album: str | None = None,
) -> dict:
    """Polls every watch entry by default. Pass only_artist (and, for an
    album-specific watch, only_album) to scope this to a single entry --
    used right after adding one, so the person doesn't wait for the next
    scheduled cycle (hours away) to see whether anything already exists."""
    conn = sqlite3.connect(str(db_path), timeout=30)
    if only_artist is not None:
        entries = conn.execute(
            "SELECT id, artist, album FROM watchlist WHERE artist=? AND album=?",
            (only_artist, only_album or ""),
        ).fetchall()
    else:
        entries = conn.execute(
            "SELECT id, artist, album FROM watchlist"
        ).fetchall()

    summary = {"checked": 0, "new_candidates": 0, "errors": 0}
    now = datetime.now(timezone.utc).isoformat()

    for watchlist_id, artist, album in entries:
        # Skip if this exact artist/album is already owned or in flight via
        # ANY path (a prior watchlist approval, the Acquire page, or a
        # review-queue search-usenet grab) -- no point spending a Prowlarr
        # call and surfacing a candidate for something already handled.
        if album:
            already = conn.execute(
                "SELECT 1 FROM review_items WHERE artist=? AND album=? LIMIT 1",
                (artist, album),
            ).fetchone()
            if already:
                continue

        try:
            releases = search_prowlarr(artist, album, prowlarr_base, api_key)
        except Exception:
            summary["errors"] += 1
            continue

        for release in releases:
            download_url = release.get("downloadUrl")
            if not download_url:
                continue
            guessed_artist, guessed_album = guess_artist_album(
                release.get("title", ""), artist, album
            )
            try:
                conn.execute(
                    "INSERT INTO watchlist_candidates "
                    "(watchlist_id, title, download_url, indexer, size, "
                    "guessed_artist, guessed_album, found_at, status) "
                    "VALUES (?,?,?,?,?,?,?,?,'pending')",
                    (watchlist_id, release.get("title", ""), download_url,
                     release.get("indexer"), release.get("size"),
                     guessed_artist or artist, guessed_album or album, now),
                )
                conn.commit()
                summary["new_candidates"] += 1
            except sqlite3.IntegrityError:
                # Already surfaced this exact release for this watch entry
                # on an earlier poll cycle -- not an error, just a repeat.
                conn.rollback()

        conn.execute(
            "UPDATE watchlist SET last_checked_at=? WHERE id=?",
            (now, watchlist_id),
        )
        conn.commit()
        summary["checked"] += 1

    conn.close()
    return summary
