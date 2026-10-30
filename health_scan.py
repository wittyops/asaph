"""Read-only library health scan — layers the shared validator's decode and
signal checks on top of the existing curator.py integrity/metadata checks,
writing results into review_items instead of only logging. Scheduled
separately (weekly, off-peak) from the existing 6h scan — see curator.py's
scheduler wiring.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from lib.validator import decode_check, signal_heuristic_check

AUDIO_EXTENSIONS = {".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".aac"}


def init_review_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS review_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT UNIQUE NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            flagged_at TEXT NOT NULL,
            resolved_at TEXT
        )
    """)
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(review_items)")}
    for col in ("artist", "album"):
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE review_items ADD COLUMN {col} TEXT")
    conn.commit()
    conn.close()


def _flag(conn: sqlite3.Connection, path: Path, reason: str) -> bool:
    """Insert if not already present with status != resolved. Returns True
    if a new row was inserted (for the caller's flagged_count).

    Rows already 'pending' or 'searching' are skipped (already flagged and
    unresolved -- don't duplicate). Rows already 'resolved' are ALSO
    skipped -- a human already reviewed and approved/handled this exact
    path (e.g. an approved false-positive clipping flag), and the weekly
    scan re-running the same heuristic against the same still-failing file
    must not silently reverse that human decision by resetting it back to
    'pending'. Only a path with no row at all, or a row in some other
    status, gets a fresh 'pending' row."""
    existing = conn.execute(
        "SELECT status FROM review_items WHERE path = ?", (str(path),)
    ).fetchone()
    if existing and existing[0] in ("pending", "searching", "resolved"):
        return False
    conn.execute(
        "INSERT OR REPLACE INTO review_items (path, reason, status, flagged_at) "
        "VALUES (?, ?, 'pending', ?)",
        (str(path), reason, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return True


def scan_library(library_paths: list[Path], db_path: Path) -> int:
    # timeout=30: tolerate a brief concurrent write from the review UI or
    # usenet_poll's own job rather than immediately raising "database is
    # locked" (Fix 4). Commits now happen per-flag inside _flag(), not once
    # at the very end of the whole scan, for the same reason -- a scan can
    # run for a long time over a large library, and holding one open
    # transaction across all of it maximizes the lock-contention window.
    conn = sqlite3.connect(str(db_path), timeout=30)
    flagged = 0
    for root in library_paths:
        if not root.exists():
            continue
        for audio_file in root.rglob("*"):
            if not audio_file.is_file() or audio_file.suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            err = decode_check(audio_file)
            if err:
                if _flag(conn, audio_file, err):
                    flagged += 1
                continue
            suspects = signal_heuristic_check(audio_file)
            if suspects:
                reason = f"{len(suspects)} suspect range(s): " + \
                    "; ".join(f"{s['kind']} at {s['start_s']:.1f}-{s['end_s']:.1f}s" for s in suspects)
                if _flag(conn, audio_file, reason):
                    flagged += 1
    conn.close()
    return flagged
