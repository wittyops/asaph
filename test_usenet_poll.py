# test_usenet_poll.py
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).parent))
from usenet_poll import poll_searching_items


def _seed_searching_row(db_path, artist="Bizzle", album="Nonstop", path=None):
    if path is None:
        path = "/library/Bizzle/Non-Album/Warriors.flac"
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
        (str(path), "failed decode scan", "searching",
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
    old_dir = tmp_path / "library" / "Bizzle" / "Nonstop"
    _seed_searching_row(db_path, path=old_dir / "01.flac")
    mock_queue.return_value = []
    mock_count.return_value = 2

    staging_dir = tmp_path / "staging" / "Bizzle" / "Nonstop"
    staging_dir.mkdir(parents=True)
    _make_valid_flac(staging_dir / "01.flac")
    _make_valid_flac(staging_dir / "02.flac")

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
    old_dir = tmp_path / "library" / "Bizzle" / "Nonstop"
    _seed_searching_row(db_path, path=old_dir / "01.flac")
    mock_queue.return_value = []
    mock_count.return_value = 3  # expects 3, staging only has 1 -- partial release

    staging_dir = tmp_path / "staging" / "Bizzle" / "Nonstop"
    staging_dir.mkdir(parents=True)
    _make_valid_flac(staging_dir / "01.flac")

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


@patch("usenet_poll._lidarr_album_track_count")
@patch("usenet_poll._lidarr_queue")
def test_lidarr_album_lookup_api_error_counts_as_error_not_still_searching(mock_queue, mock_count, tmp_path):
    """A genuine API failure in the artist/album lookup (e.g. Lidarr auth
    failure or 500 on /api/v1/artist or /api/v1/album) must surface as an
    'errors' outcome, not get folded into 'still_searching' alongside the
    legitimate not-found/ambiguous case."""
    db_path = tmp_path / "review.db"
    _seed_searching_row(db_path)
    mock_queue.return_value = []  # no longer in Lidarr's active queue
    mock_count.side_effect = Exception("Lidarr API 500")

    staging_dir = tmp_path / "staging" / "Bizzle" / "Nonstop"
    staging_dir.mkdir(parents=True)
    _make_valid_flac(staging_dir / "01.flac")

    result = poll_searching_items(
        db_path, "http://lidarr:8686", "fakekey",
        tmp_path / "staging", tmp_path / "library", tmp_path / "superseded",
    )

    assert result == {"resolved": 0, "still_searching": 0, "errors": 1}
    status = sqlite3.connect(str(db_path)).execute(
        "SELECT status FROM review_items").fetchone()[0]
    assert status == "searching"  # unchanged, not corrupted by the error


@patch("usenet_poll._lidarr_album_track_count")
@patch("usenet_poll._lidarr_queue")
def test_supersede_uses_real_path_not_reconstructed_artist_album_dir(mock_queue, mock_count, tmp_path):
    """The library's real directory names go through ingress_sweeper.py's
    sanitize() (colon -> full-width colon, etc). A raw artist/album string
    from the review form does not reliably match that real on-disk name.
    old_dir must come from the row's own `path` column (the real file that
    was actually flagged), not from reconstructing library_root/artist/album."""
    db_path = tmp_path / "review.db"
    artist = "AphroDite: Queen"   # raw form value -- has a literal colon
    album = "Greatest Hits"
    # Real on-disk directory name, as ingress_sweeper.sanitize() would have
    # produced it: colon -> full-width colon.
    real_old_dir = tmp_path / "library" / "AphroDite： Queen" / "Greatest Hits"
    _seed_searching_row(db_path, artist=artist, album=album, path=real_old_dir / "01.flac")
    mock_queue.return_value = []
    mock_count.return_value = 1

    # Staging dir construction is unchanged by this fix (out of scope --
    # folder-naming mismatch there is a separate, not-yet-decided issue) --
    # it's still built from the raw artist/album strings.
    staging_dir = tmp_path / "staging" / artist / album
    staging_dir.mkdir(parents=True)
    _make_valid_flac(staging_dir / "01.flac")

    real_old_dir.mkdir(parents=True)
    (real_old_dir / "01.flac").write_bytes(b"old-bad-rip")

    # The WRONG directory a naive library_root/artist/album reconstruction
    # would have targeted (raw colon, no sanitization). Must never be
    # created or touched by this run.
    wrong_old_dir = tmp_path / "library" / artist / album

    result = poll_searching_items(
        db_path, "http://lidarr:8686", "fakekey",
        tmp_path / "staging", tmp_path / "library", tmp_path / "superseded",
    )

    assert result == {"resolved": 1, "still_searching": 0, "errors": 0}
    assert not wrong_old_dir.exists()
    # The real (sanitized-name) directory now holds the promoted file.
    assert real_old_dir.exists()
    assert (real_old_dir / "01.flac").exists()
    assert (real_old_dir / "01.flac").read_bytes() != b"old-bad-rip"
    archived = list((tmp_path / "superseded").rglob("*.flac"))
    assert len(archived) >= 1


@patch("usenet_poll._lidarr_album_track_count")
@patch("usenet_poll._lidarr_queue")
def test_one_row_update_failure_does_not_lose_other_rows_committed_same_cycle(mock_queue, mock_count, tmp_path):
    """poll_searching_items commits per-row (Fix 4). If resolving one row's
    UPDATE statement fails, rows already resolved earlier in the same batch
    must stay committed/resolved rather than being rolled back with it."""
    db_path = tmp_path / "review.db"

    old_dir_a = tmp_path / "library" / "ArtistA" / "AlbumA"
    row_a = _seed_searching_row(db_path, artist="ArtistA", album="AlbumA", path=old_dir_a / "01.flac")
    old_dir_b = tmp_path / "library" / "ArtistB" / "AlbumB"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO review_items (path, reason, status, flagged_at, artist, album) "
        "VALUES (?,?,?,?,?,?)",
        (str(old_dir_b / "01.flac"), "failed decode scan", "searching",
         datetime.now(timezone.utc).isoformat(), "ArtistB", "AlbumB"),
    )
    conn.commit()
    row_b = conn.execute(
        "SELECT id FROM review_items WHERE artist='ArtistB'"
    ).fetchone()[0]
    conn.close()

    mock_queue.return_value = []
    mock_count.return_value = 1

    for artist, album, old_dir in (("ArtistA", "AlbumA", old_dir_a), ("ArtistB", "AlbumB", old_dir_b)):
        staging_dir = tmp_path / "staging" / artist / album
        staging_dir.mkdir(parents=True)
        _make_valid_flac(staging_dir / "01.flac")
        old_dir.mkdir(parents=True)
        (old_dir / "01.flac").write_bytes(b"old-bad-rip")

    # Row A resolves normally. Row B's UPDATE fails -- simulate via a thin
    # wrapper around the real connection (sqlite3.Connection is a C type and
    # can't be monkeypatched directly) that raises only on the 2nd UPDATE.
    call_state = {"updates_seen": 0}

    class FlakyConnWrapper:
        def __init__(self, real_conn):
            self._real = real_conn

        def execute(self, sql, params=()):
            if sql.strip().upper().startswith("UPDATE"):
                call_state["updates_seen"] += 1
                if call_state["updates_seen"] == 2:
                    raise sqlite3.OperationalError("simulated lock timeout")
            return self._real.execute(sql, params)

        def commit(self):
            return self._real.commit()

        def close(self):
            return self._real.close()

    real_connect = sqlite3.connect

    def flaky_connect(*args, **kwargs):
        return FlakyConnWrapper(real_connect(*args, **kwargs))

    with patch("usenet_poll.sqlite3.connect", flaky_connect):
        result = poll_searching_items(
            db_path, "http://lidarr:8686", "fakekey",
            tmp_path / "staging", tmp_path / "library", tmp_path / "superseded",
        )

    assert result["errors"] == 1
    assert result["resolved"] == 1

    conn = sqlite3.connect(str(db_path))
    status_a = conn.execute("SELECT status FROM review_items WHERE id=?", (row_a,)).fetchone()[0]
    status_b = conn.execute("SELECT status FROM review_items WHERE id=?", (row_b,)).fetchone()[0]
    conn.close()
    # Row A's resolution must have survived row B's later failure in the
    # same cycle -- proves per-row commit, not one commit at the very end.
    assert status_a == "resolved"
    assert status_b == "searching"
