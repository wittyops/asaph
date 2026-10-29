# test_usenet_poll.py
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).parent))
from usenet_poll import poll_searching_items


def _seed_searching_row(db_path, artist="Bizzle", album="Nonstop"):
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS review_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT UNIQUE NOT NULL,
            reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            flagged_at TEXT NOT NULL, resolved_at TEXT,
            artist TEXT, album TEXT
        )
    """)
    conn.execute(
        "INSERT INTO review_items (path, reason, status, flagged_at, artist, album) "
        "VALUES (?,?,?,?,?,?)",
        ("/library/Bizzle/Non-Album/Warriors.flac", "failed decode scan", "searching",
         datetime.now(timezone.utc).isoformat(), artist, album),
    )
    conn.commit()
    row_id = conn.execute("SELECT id FROM review_items").fetchone()[0]
    conn.close()
    return row_id


def _make_valid_flac(path):
    subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
         "-t", "1", "-y", str(path)],
        check=True, capture_output=True,
    )


@patch("usenet_poll._lidarr_queue")
def test_item_still_in_lidarr_queue_stays_searching(mock_queue, tmp_path):
    db_path = tmp_path / "review.db"
    _seed_searching_row(db_path)
    mock_queue.return_value = [{"artist": {"artistName": "Bizzle"}, "album": {"title": "Nonstop"}}]

    result = poll_searching_items(
        db_path, "http://lidarr:8686", "fakekey",
        tmp_path / "staging", tmp_path / "library", tmp_path / "superseded",
    )

    assert result == {"resolved": 0, "still_searching": 1, "errors": 0}
    status = sqlite3.connect(str(db_path)).execute(
        "SELECT status FROM review_items").fetchone()[0]
    assert status == "searching"


@patch("usenet_poll._lidarr_album_track_count")
@patch("usenet_poll._lidarr_queue")
def test_item_left_out_of_queue_without_staging_dir_stays_searching(mock_queue, mock_count, tmp_path):
    db_path = tmp_path / "review.db"
    _seed_searching_row(db_path)
    mock_queue.return_value = []  # no longer in Lidarr's active queue
    mock_count.return_value = 2
    # no staging dir created -- ambiguous, must not guess

    result = poll_searching_items(
        db_path, "http://lidarr:8686", "fakekey",
        tmp_path / "staging", tmp_path / "library", tmp_path / "superseded",
    )

    assert result == {"resolved": 0, "still_searching": 1, "errors": 0}


@patch("usenet_poll._lidarr_album_track_count")
@patch("usenet_poll._lidarr_queue")
def test_complete_staging_dir_resolves_and_supersedes(mock_queue, mock_count, tmp_path):
    db_path = tmp_path / "review.db"
    _seed_searching_row(db_path)
    mock_queue.return_value = []
    mock_count.return_value = 2

    staging_dir = tmp_path / "staging" / "Bizzle" / "Nonstop"
    staging_dir.mkdir(parents=True)
    _make_valid_flac(staging_dir / "01.flac")
    _make_valid_flac(staging_dir / "02.flac")

    old_dir = tmp_path / "library" / "Bizzle" / "Nonstop"
    old_dir.mkdir(parents=True)
    (old_dir / "01.flac").write_bytes(b"old-bad-rip")

    result = poll_searching_items(
        db_path, "http://lidarr:8686", "fakekey",
        tmp_path / "staging", tmp_path / "library", tmp_path / "superseded",
    )

    assert result == {"resolved": 1, "still_searching": 0, "errors": 0}
    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT status, reason FROM review_items").fetchone()
    assert row[0] == "resolved"
    assert "superseded" in row[1].lower()
    promoted = tmp_path / "library" / "Bizzle" / "Nonstop"
    assert (promoted / "01.flac").exists()
    archived = list((tmp_path / "superseded").rglob("*.flac"))
    assert len(archived) >= 1


@patch("usenet_poll._lidarr_album_track_count")
@patch("usenet_poll._lidarr_queue")
def test_incomplete_staging_dir_resolves_without_superseding(mock_queue, mock_count, tmp_path):
    db_path = tmp_path / "review.db"
    _seed_searching_row(db_path)
    mock_queue.return_value = []
    mock_count.return_value = 3  # expects 3, staging only has 1 -- partial release

    staging_dir = tmp_path / "staging" / "Bizzle" / "Nonstop"
    staging_dir.mkdir(parents=True)
    _make_valid_flac(staging_dir / "01.flac")

    old_dir = tmp_path / "library" / "Bizzle" / "Nonstop"
    old_dir.mkdir(parents=True)
    (old_dir / "01.flac").write_bytes(b"old-bad-rip")

    result = poll_searching_items(
        db_path, "http://lidarr:8686", "fakekey",
        tmp_path / "staging", tmp_path / "library", tmp_path / "superseded",
    )

    assert result == {"resolved": 1, "still_searching": 0, "errors": 0}
    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT status, reason FROM review_items").fetchone()
    assert row[0] == "resolved"
    assert "incomplete" in row[1].lower() or "partial" in row[1].lower()
    # old library file must NOT have been touched
    assert (tmp_path / "library" / "Bizzle" / "Nonstop" / "01.flac").read_bytes() == b"old-bad-rip"


@patch("usenet_poll._lidarr_album_track_count")
@patch("usenet_poll._lidarr_queue")
def test_lidarr_api_error_leaves_row_searching_not_erroring_the_batch(mock_queue, mock_count, tmp_path):
    db_path = tmp_path / "review.db"
    _seed_searching_row(db_path)
    mock_queue.side_effect = Exception("connection refused")

    result = poll_searching_items(
        db_path, "http://lidarr:8686", "fakekey",
        tmp_path / "staging", tmp_path / "library", tmp_path / "superseded",
    )

    assert result == {"resolved": 0, "still_searching": 0, "errors": 1}
    status = sqlite3.connect(str(db_path)).execute(
        "SELECT status FROM review_items").fetchone()[0]
    assert status == "searching"  # unchanged, not corrupted by the error
