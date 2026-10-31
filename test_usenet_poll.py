# test_usenet_poll.py
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).parent))
import mutagen
import mutagen.flac
from usenet_poll import poll_searching_items


SABNZBD_PREFIX = Path("/data/usenet/complete/asaph-staging")


def _seed_searching_row(db_path, artist="Bizzle", album="Nonstop", path=None, nzo_id: str | None = "nzo-1"):
    if path is None:
        path = "/library/Bizzle/Non-Album/Warriors.flac"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS review_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT UNIQUE NOT NULL,
            reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            flagged_at TEXT NOT NULL, resolved_at TEXT,
            artist TEXT, album TEXT, nzo_id TEXT
        )
    """)
    conn.execute(
        "INSERT INTO review_items (path, reason, status, flagged_at, artist, album, nzo_id) "
        "VALUES (?,?,?,?,?,?,?)",
        (str(path), "failed decode scan", "searching",
         datetime.now(timezone.utc).isoformat(), artist, album, nzo_id),
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


def _make_real_jpeg_bytes(tmp_path) -> bytes:
    """A deliberately-truncated/fake JPEG (just SOI/EOI marker bytes) is
    fine for tests that only read the picture back via mutagen, but ffmpeg
    treats an embedded picture as its own (attached_pic) stream and probes
    it during decode -- a malformed one makes decode_check() falsely flag
    the whole file as corrupt even though the AUDIO is perfectly fine
    (confirmed live 2026-10-31: real, valid album art does not trigger
    this). Tests here run decode_check() on the result via
    check_completeness(), so they need a real one."""
    out = tmp_path / "_fixture_art.jpg"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=16x16",
         "-frames:v", "1", str(out)],
        check=True, capture_output=True,
    )
    return out.read_bytes()


def _make_tagged_flac(path, title="Track", artist="Bizzle", album="Nonstop"):
    """Like _make_valid_flac but with tags + embedded art, for metadata-
    check tests specifically -- _make_valid_flac is deliberately bare for
    every other test in this file, which don't care about metadata."""
    _make_valid_flac(path)
    audio = mutagen.File(str(path), easy=True)
    audio["title"] = title
    audio["artist"] = artist
    audio["album"] = album
    audio.save()
    flac = mutagen.flac.FLAC(str(path))
    pic = mutagen.flac.Picture()
    pic.data = _make_real_jpeg_bytes(path.parent)
    pic.type = 3
    pic.mime = "image/jpeg"
    flac.add_picture(pic)
    flac.save()


def _poll(db_path, tmp_path, **kwargs):
    return poll_searching_items(
        db_path, "http://sabnzbd:8080", "fakekey",
        tmp_path / "staging", tmp_path / "library", tmp_path / "superseded",
        SABNZBD_PREFIX,
        **kwargs,
    )


@patch("usenet_poll.sabnzbd_queue")
def test_item_still_in_sabnzbd_queue_stays_searching(mock_queue, tmp_path):
    db_path = tmp_path / "review.db"
    _seed_searching_row(db_path)
    mock_queue.return_value = [{"nzo_id": "nzo-1", "status": "Downloading"}]

    result = _poll(db_path, tmp_path)

    assert result == {"resolved": 0, "still_searching": 1, "errors": 0}
    status = sqlite3.connect(str(db_path)).execute(
        "SELECT status FROM review_items").fetchone()[0]
    assert status == "searching"


@patch("usenet_poll.sabnzbd_history")
@patch("usenet_poll.sabnzbd_queue")
def test_item_not_in_queue_or_history_stays_searching(mock_queue, mock_history, tmp_path):
    db_path = tmp_path / "review.db"
    _seed_searching_row(db_path)
    mock_queue.return_value = []
    mock_history.return_value = []  # not known anywhere -- never guess

    result = _poll(db_path, tmp_path)

    assert result == {"resolved": 0, "still_searching": 1, "errors": 0}


@patch("usenet_poll.musicbrainz_track_count")
@patch("usenet_poll.sabnzbd_history")
@patch("usenet_poll.sabnzbd_queue")
def test_complete_history_resolves_and_supersedes(mock_queue, mock_history, mock_count, tmp_path):
    db_path = tmp_path / "review.db"
    old_dir = tmp_path / "library" / "Bizzle" / "Nonstop"
    _seed_searching_row(db_path, path=old_dir / "01.flac")
    mock_queue.return_value = []
    mock_count.return_value = 2

    sab_dir = SABNZBD_PREFIX / "Some.Release.Folder"
    real_sab_dir = tmp_path / "staging" / "Some.Release.Folder"
    real_sab_dir.mkdir(parents=True)
    # Tagged, not _make_valid_flac -- this test is about supersede
    # mechanics, not metadata; an untagged fixture would trip the
    # post-promotion metadata check and leave the row 'pending' instead
    # of 'resolved', which is a different test (see
    # test_promoted_file_with_missing_metadata_stays_pending_for_review).
    _make_tagged_flac(real_sab_dir / "01.flac")
    _make_tagged_flac(real_sab_dir / "02.flac")
    mock_history.return_value = [
        {"nzo_id": "nzo-1", "status": "Completed", "storage": str(sab_dir)},
    ]

    old_dir.mkdir(parents=True)
    (old_dir / "01.flac").write_bytes(b"old-bad-rip")

    # remap_sabnzbd_path strips SABNZBD_PREFIX and joins onto staging_root;
    # staging_root here is tmp_path/"staging", so the remapped path is
    # tmp_path/"staging"/"Some.Release.Folder" -- which is where we built
    # the fake SABnzbd-completed folder above.
    result = _poll(db_path, tmp_path)

    assert result == {"resolved": 1, "still_searching": 0, "errors": 0}
    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT status, reason FROM review_items").fetchone()
    assert row[0] == "resolved"
    assert "superseded" in row[1].lower()
    promoted = tmp_path / "library" / "Bizzle" / "Nonstop"
    assert (promoted / "01.flac").exists()
    archived = list((tmp_path / "superseded").rglob("*.flac"))
    assert len(archived) >= 1


@patch("usenet_poll.musicbrainz_track_count")
@patch("usenet_poll.sabnzbd_history")
@patch("usenet_poll.sabnzbd_queue")
def test_incomplete_grab_resolves_without_superseding(mock_queue, mock_history, mock_count, tmp_path):
    db_path = tmp_path / "review.db"
    old_dir = tmp_path / "library" / "Bizzle" / "Nonstop"
    _seed_searching_row(db_path, path=old_dir / "01.flac")
    mock_queue.return_value = []
    mock_count.return_value = 3  # expects 3, staging only has 1 -- partial release

    sab_dir = SABNZBD_PREFIX / "Some.Release.Folder"
    real_sab_dir = tmp_path / "staging" / "Some.Release.Folder"
    real_sab_dir.mkdir(parents=True)
    _make_valid_flac(real_sab_dir / "01.flac")
    mock_history.return_value = [
        {"nzo_id": "nzo-1", "status": "Completed", "storage": str(sab_dir)},
    ]

    old_dir.mkdir(parents=True)
    (old_dir / "01.flac").write_bytes(b"old-bad-rip")

    result = _poll(db_path, tmp_path)

    assert result == {"resolved": 1, "still_searching": 0, "errors": 0}
    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT status, reason FROM review_items").fetchone()
    assert row[0] == "resolved"
    assert "incomplete" in row[1].lower() or "partial" in row[1].lower()
    # old library file must NOT have been touched
    assert (tmp_path / "library" / "Bizzle" / "Nonstop" / "01.flac").read_bytes() == b"old-bad-rip"


@patch("usenet_poll.sabnzbd_history")
@patch("usenet_poll.sabnzbd_queue")
def test_failed_history_status_resolves_without_superseding(mock_queue, mock_history, tmp_path):
    db_path = tmp_path / "review.db"
    old_dir = tmp_path / "library" / "Bizzle" / "Nonstop"
    _seed_searching_row(db_path, path=old_dir / "01.flac")
    mock_queue.return_value = []
    mock_history.return_value = [
        {"nzo_id": "nzo-1", "status": "Failed", "storage": None, "fail_message": "bad crc"},
    ]

    old_dir.mkdir(parents=True)
    (old_dir / "01.flac").write_bytes(b"old-bad-rip")

    result = _poll(db_path, tmp_path)

    assert result == {"resolved": 1, "still_searching": 0, "errors": 0}
    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT status, reason FROM review_items").fetchone()
    assert row[0] == "resolved"
    assert "incomplete" in row[1].lower() or "partial" in row[1].lower()
    assert (old_dir / "01.flac").read_bytes() == b"old-bad-rip"


@patch("usenet_poll.sabnzbd_queue")
def test_sabnzbd_api_error_leaves_row_searching_not_erroring_the_batch(mock_queue, tmp_path):
    db_path = tmp_path / "review.db"
    _seed_searching_row(db_path)
    mock_queue.side_effect = Exception("connection refused")

    result = _poll(db_path, tmp_path)

    assert result == {"resolved": 0, "still_searching": 0, "errors": 1}
    status = sqlite3.connect(str(db_path)).execute(
        "SELECT status FROM review_items").fetchone()[0]
    assert status == "searching"  # unchanged, not corrupted by the error


@patch("usenet_poll.musicbrainz_track_count")
@patch("usenet_poll.sabnzbd_history")
@patch("usenet_poll.sabnzbd_queue")
def test_musicbrainz_lookup_api_error_counts_as_error_not_still_searching(mock_queue, mock_history, mock_count, tmp_path):
    """A genuine API failure in the MusicBrainz track-count lookup must
    surface as an 'errors' outcome, not get folded into 'still_searching'
    alongside the legitimate not-found/ambiguous case."""
    db_path = tmp_path / "review.db"
    old_dir = tmp_path / "library" / "Bizzle" / "Nonstop"
    _seed_searching_row(db_path, path=old_dir / "01.flac")
    mock_queue.return_value = []
    mock_count.side_effect = Exception("MusicBrainz API 500")

    sab_dir = SABNZBD_PREFIX / "Some.Release.Folder"
    real_sab_dir = tmp_path / "staging" / "Some.Release.Folder"
    real_sab_dir.mkdir(parents=True)
    # Tagged -- this test is about the MusicBrainz retry path, not
    # metadata; an untagged fixture would leave the row 'pending' on the
    # second (successful) cycle instead of 'resolved'.
    _make_tagged_flac(real_sab_dir / "01.flac")
    mock_history.return_value = [
        {"nzo_id": "nzo-1", "status": "Completed", "storage": str(sab_dir)},
    ]

    result = _poll(db_path, tmp_path)

    assert result == {"resolved": 0, "still_searching": 0, "errors": 1}
    status = sqlite3.connect(str(db_path)).execute(
        "SELECT status FROM review_items").fetchone()[0]
    assert status == "searching"  # unchanged, not corrupted by the error

    # Regression (review finding, commit 5bcafea): the SABnzbd-side source
    # folder must be left completely untouched when the track-count lookup
    # fails -- rename_into_staging_layout() must not run until AFTER a real
    # expected_count is in hand. Previously the rename ran first, emptying
    # real_sab_dir; a second poll cycle would then crash with
    # FileNotFoundError trying to re-read a folder that no longer existed,
    # permanently stranding the row even though its files were already safe.
    assert real_sab_dir.exists()
    assert (real_sab_dir / "01.flac").exists()

    # A second cycle, once MusicBrainz is reachable again, must resolve
    # cleanly from the same untouched source -- proving the row isn't
    # permanently stuck.
    mock_count.side_effect = None
    mock_count.return_value = 1
    result2 = _poll(db_path, tmp_path)
    assert result2 == {"resolved": 1, "still_searching": 0, "errors": 0}
    status2 = sqlite3.connect(str(db_path)).execute(
        "SELECT status FROM review_items").fetchone()[0]
    assert status2 == "resolved"


@patch("usenet_poll.musicbrainz_track_count")
@patch("usenet_poll.sabnzbd_history")
@patch("usenet_poll.sabnzbd_queue")
def test_supersede_uses_real_path_not_reconstructed_artist_album_dir(mock_queue, mock_history, mock_count, tmp_path):
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

    sab_dir = SABNZBD_PREFIX / "Some.Release.Folder"
    real_sab_dir = tmp_path / "staging" / "Some.Release.Folder"
    real_sab_dir.mkdir(parents=True)
    _make_valid_flac(real_sab_dir / "01.flac")
    mock_history.return_value = [
        {"nzo_id": "nzo-1", "status": "Completed", "storage": str(sab_dir)},
    ]

    real_old_dir.mkdir(parents=True)
    (real_old_dir / "01.flac").write_bytes(b"old-bad-rip")

    # The WRONG directory a naive library_root/artist/album reconstruction
    # would have targeted (raw colon, no sanitization). Must never be
    # created or touched by this run.
    wrong_old_dir = tmp_path / "library" / artist / album

    result = _poll(db_path, tmp_path)

    assert result == {"resolved": 1, "still_searching": 0, "errors": 0}
    assert not wrong_old_dir.exists()
    # The real (sanitized-name) directory now holds the promoted file.
    assert real_old_dir.exists()
    assert (real_old_dir / "01.flac").exists()
    assert (real_old_dir / "01.flac").read_bytes() != b"old-bad-rip"
    archived = list((tmp_path / "superseded").rglob("*.flac"))
    assert len(archived) >= 1


@patch("usenet_poll.musicbrainz_track_count")
@patch("usenet_poll.sabnzbd_history")
@patch("usenet_poll.sabnzbd_queue")
def test_one_row_update_failure_does_not_lose_other_rows_committed_same_cycle(mock_queue, mock_history, mock_count, tmp_path):
    """poll_searching_items commits per-row (Fix 4). If resolving one row's
    UPDATE statement fails, rows already resolved earlier in the same batch
    must stay committed/resolved rather than being rolled back with it."""
    db_path = tmp_path / "review.db"

    old_dir_a = tmp_path / "library" / "ArtistA" / "AlbumA"
    row_a = _seed_searching_row(db_path, artist="ArtistA", album="AlbumA", path=old_dir_a / "01.flac", nzo_id="nzo-a")
    old_dir_b = tmp_path / "library" / "ArtistB" / "AlbumB"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO review_items (path, reason, status, flagged_at, artist, album, nzo_id) "
        "VALUES (?,?,?,?,?,?,?)",
        (str(old_dir_b / "01.flac"), "failed decode scan", "searching",
         datetime.now(timezone.utc).isoformat(), "ArtistB", "AlbumB", "nzo-b"),
    )
    conn.commit()
    row_b = conn.execute(
        "SELECT id FROM review_items WHERE artist='ArtistB'"
    ).fetchone()[0]
    conn.close()

    mock_queue.return_value = []
    mock_count.return_value = 1

    sab_dirs = {}
    for suffix, old_dir in (("a", old_dir_a), ("b", old_dir_b)):
        sab_dir_name = f"Some.Release.Folder.{suffix}"
        real_sab_dir = tmp_path / "staging" / sab_dir_name
        real_sab_dir.mkdir(parents=True)
        # Tagged -- this test is about per-row commit isolation under a
        # flaky connection, not metadata; an untagged fixture would leave
        # row A 'pending' instead of 'resolved', which is a different test.
        _make_tagged_flac(real_sab_dir / "01.flac")
        sab_dirs[suffix] = SABNZBD_PREFIX / sab_dir_name
        old_dir.mkdir(parents=True)
        (old_dir / "01.flac").write_bytes(b"old-bad-rip")

    mock_history.return_value = [
        {"nzo_id": "nzo-a", "status": "Completed", "storage": str(sab_dirs["a"])},
        {"nzo_id": "nzo-b", "status": "Completed", "storage": str(sab_dirs["b"])},
    ]

    # Row A resolves normally (1 UPDATE -- resolution + the metadata-check
    # suffix are a single combined write). Row B's resolve UPDATE (the 2nd
    # UPDATE overall) fails -- simulate via a thin wrapper around the real
    # connection (sqlite3.Connection is a C type and can't be monkeypatched
    # directly).
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
        result = _poll(db_path, tmp_path)

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


def test_row_without_nzo_id_stays_searching(tmp_path):
    """A row with status='searching' but no nzo_id (e.g. left over from a
    schema before this column existed, or an unexpected gap) must never be
    guessed at -- leave it alone rather than crash or silently resolve it."""
    db_path = tmp_path / "review.db"
    _seed_searching_row(db_path, nzo_id=None)

    result = _poll(db_path, tmp_path)

    assert result == {"resolved": 0, "still_searching": 1, "errors": 0}


@patch("usenet_poll.musicbrainz_track_count")
@patch("usenet_poll.sabnzbd_history")
@patch("usenet_poll.sabnzbd_queue")
def test_promoted_file_with_clean_metadata_does_not_flag_a_new_row(mock_queue, mock_history, mock_count, tmp_path):
    db_path = tmp_path / "review.db"
    old_dir = tmp_path / "library" / "Bizzle" / "Nonstop"
    _seed_searching_row(db_path, path=old_dir / "01.flac")
    mock_queue.return_value = []
    mock_count.return_value = 1

    sab_dir = SABNZBD_PREFIX / "Some.Release.Folder"
    real_sab_dir = tmp_path / "staging" / "Some.Release.Folder"
    real_sab_dir.mkdir(parents=True)
    _make_tagged_flac(real_sab_dir / "01.flac")
    mock_history.return_value = [
        {"nzo_id": "nzo-1", "status": "Completed", "storage": str(sab_dir)},
    ]
    old_dir.mkdir(parents=True)
    (old_dir / "01.flac").write_bytes(b"old-bad-rip")

    result = _poll(db_path, tmp_path)

    assert result == {"resolved": 1, "still_searching": 0, "errors": 0}
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT status, reason FROM review_items").fetchall()
    conn.close()
    assert len(rows) == 1, "clean metadata must not flag a second review_items row"
    assert "[metadata check: clean]" in rows[0][1]


@patch("usenet_poll.musicbrainz_track_count")
@patch("usenet_poll.sabnzbd_history")
@patch("usenet_poll.sabnzbd_queue")
def test_promoted_file_with_missing_metadata_stays_pending_for_review(mock_queue, mock_history, mock_count, tmp_path):
    """A metadata gap must NOT spawn a second review_items row -- the
    promoted file sits at the exact path the original row's own `path`
    UNIQUE constraint already owns, so a second INSERT there always
    violates it. Instead the SAME row stays 'pending' (not 'resolved'),
    flowing back through the normal review queue for a relabel/art fix."""
    db_path = tmp_path / "review.db"
    old_dir = tmp_path / "library" / "Bizzle" / "Nonstop"
    row_id = _seed_searching_row(db_path, path=old_dir / "01.flac")
    mock_queue.return_value = []
    mock_count.return_value = 1

    sab_dir = SABNZBD_PREFIX / "Some.Release.Folder"
    real_sab_dir = tmp_path / "staging" / "Some.Release.Folder"
    real_sab_dir.mkdir(parents=True)
    _make_valid_flac(real_sab_dir / "01.flac")  # untagged, no art -- deliberate
    mock_history.return_value = [
        {"nzo_id": "nzo-1", "status": "Completed", "storage": str(sab_dir)},
    ]
    old_dir.mkdir(parents=True)
    (old_dir / "01.flac").write_bytes(b"old-bad-rip")

    result = _poll(db_path, tmp_path)

    # The grab+promotion itself succeeded -- "resolved" counts the usenet
    # side of this row finishing, independent of whether the row's own DB
    # status ends up 'resolved' or stays 'pending' for a metadata fix.
    assert result == {"resolved": 1, "still_searching": 0, "errors": 0}
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT id, status, reason, path FROM review_items").fetchall()
    conn.close()
    assert len(rows) == 1, "a metadata gap must reuse the same row, never spawn a second"

    row = rows[0]
    assert row[0] == row_id
    assert row[1] == "pending"
    assert "superseded old library files" in row[2]
    assert "track(s) have gaps" in row[2]
    assert "needs relabel/art fix" in row[2]
    assert row[3] == str(old_dir / "01.flac")
    # The promoted file is really there on disk, pending status or not.
    assert (old_dir / "01.flac").exists()
