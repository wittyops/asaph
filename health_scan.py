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
    conn = sqlite3.connect(str(db_path))
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
    conn.commit()
    conn.close()


def _flag(conn: sqlite3.Connection, path: Path, reason: str) -> bool:
    """Insert if not already present with status != resolved. Returns True
    if a new row was inserted (for the caller's flagged_count)."""
    existing = conn.execute(
        "SELECT status FROM review_items WHERE path = ?", (str(path),)
    ).fetchone()
    if existing and existing[0] in ("pending", "searching"):
        return False  # already flagged and unresolved — don't duplicate
    conn.execute(
        "INSERT OR REPLACE INTO review_items (path, reason, status, flagged_at) "
        "VALUES (?, ?, 'pending', ?)",
        (str(path), reason, datetime.now(timezone.utc).isoformat()),
    )
    return True


def scan_library(library_paths: list[Path], db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
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
    conn.commit()
    conn.close()
    return flagged
