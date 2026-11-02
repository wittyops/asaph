import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).parent))
from ingress_sweeper import sweep


def _make_truncated_file(tmp_path) -> Path:
    good = tmp_path / "good.flac"
    subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
         "-t", "2", "-y", str(good)],
        check=True, capture_output=True,
    )
    bad = tmp_path / "truncated.flac"
    bad.write_bytes(good.read_bytes()[:200])
    good.unlink()  # only the truncated file should land in ingress/
    return bad


def test_decode_failure_quarantines_as_corrupt(tmp_path):
    ingress = tmp_path / "ingress"
    library = tmp_path / "library"
    ingress.mkdir()
    library.mkdir()
    _make_truncated_file(ingress)

    report = sweep(ingress, library, live=True)

    assert len(report.quarantined) == 1
    assert report.quarantined[0].status == "quarantined-corrupt"


def _make_tagged_flac(path, title="Test Title", artist="Test Artist"):
    subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "2",
         "-metadata", f"title={title}", "-metadata", f"artist={artist}",
         "-metadata", "album=Test Album",
         "-metadata:s:v", "comment=Cover (front)",
         "-y", str(path)],
        check=True, capture_output=True,
    )


def test_clipping_suspect_with_clean_decode_moves_not_quarantines(tmp_path):
    """signal_heuristic_check is a heuristic, documented as 'never a
    pass/fail verdict on its own' -- only decode_check is a real hard gate.
    A file with a clipping suspect but a clean decode must still move into
    the library (status 'moved'), not get hard-quarantined as
    'quarantined-suspect' -- that would leave most normal modern-mastered
    or lossy-decoded audio stuck forever at ingest."""
    ingress = tmp_path / "ingress"
    library = tmp_path / "library"
    ingress.mkdir()
    library.mkdir()
    track = ingress / "track.flac"
    _make_tagged_flac(track)

    with patch("ingress_sweeper.signal_heuristic_check") as mock_suspects, \
         patch("ingress_sweeper.decode_check", return_value=None):
        mock_suspects.return_value = [
            {"start_s": 1.0, "end_s": 2.0, "kind": "clipping"}
        ]
        # Embedded art isn't produced by the ffmpeg metadata flags above, so
        # also patch has_art detection via a sibling cover file instead of
        # fighting mutagen's real art-reading path.
        (ingress / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
        report = sweep(ingress, library, live=True)

    assert len(report.quarantined) == 0
    assert len(report.moved) == 1
    assert report.moved[0].status == "moved"
    assert "clipping" in report.moved[0].reason.lower()
    assert report.moved[0].dest.exists()


def test_live_mode_flags_quarantined_item_in_review_db(tmp_path):
    """A quarantined item (anything NOT moved/skipped-dupe) must produce a
    review_items row with the right reason, via the exact same
    health_scan.init_review_db/_flag mechanism health_scan.py and the review
    UI already use -- not a separate, parallel DB-writing path."""
    ingress = tmp_path / "ingress"
    library = tmp_path / "library"
    ingress.mkdir()
    library.mkdir()
    bad = _make_truncated_file(ingress)
    review_db = tmp_path / "state" / "review.db"

    report = sweep(ingress, library, live=True, review_db=review_db)

    assert len(report.quarantined) == 1
    assert review_db.exists()
    conn = sqlite3.connect(str(review_db))
    row = conn.execute(
        "SELECT path, reason, status FROM review_items WHERE path = ?",
        (str(bad),),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[2] == "pending"


def test_same_duration_different_title_is_not_a_dupe(tmp_path):
    """Regression for 2026-11-02: existing_dupe() matched on duration alone,
    so any two same-length tracks in the same album dir (routine for a real
    album) false-positived as duplicates -- a real transfer saw 244/248
    tracks skipped this way, 0 moved. Two different, same-length tracks by
    the same artist/album must both move."""
    ingress = tmp_path / "ingress"
    library = tmp_path / "library"
    ingress.mkdir()
    library.mkdir()

    # Pre-existing library track: same artist/album, same duration, DIFFERENT title.
    existing_dir = library / "Test Artist" / "Test Album"
    existing_dir.mkdir(parents=True)
    _make_tagged_flac(existing_dir / "01 - Existing Track.flac",
                       title="Existing Track", artist="Test Artist")

    # Incoming: same artist/album/duration, but a genuinely different song.
    incoming = ingress / "New Track.flac"
    _make_tagged_flac(incoming, title="New Track", artist="Test Artist")
    (ingress / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0fakejpeg")

    report = sweep(ingress, library, live=True)

    assert len(report.skipped_dupe) == 0, \
        f"false-positive dupe match: {[ (i.src, i.reason) for i in report.skipped_dupe ]}"
    assert len(report.moved) == 1
    assert report.moved[0].dest.exists()


def test_same_duration_same_title_different_extension_is_a_dupe(tmp_path):
    """The case existing_dupe() is actually meant to catch: the same track
    re-encoded/re-tagged with a different filename/extension landing in the
    same album dir. Must still be correctly skipped."""
    ingress = tmp_path / "ingress"
    library = tmp_path / "library"
    ingress.mkdir()
    library.mkdir()

    existing_dir = library / "Test Artist" / "Test Album"
    existing_dir.mkdir(parents=True)
    _make_tagged_flac(existing_dir / "01 - Same Track.flac",
                       title="Same Track", artist="Test Artist")

    incoming = ingress / "Same Track (re-rip).flac"
    _make_tagged_flac(incoming, title="Same Track", artist="Test Artist")
    (ingress / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0fakejpeg")

    report = sweep(ingress, library, live=True)

    assert len(report.moved) == 0
    assert len(report.skipped_dupe) == 1


def test_dry_run_does_not_write_review_db(tmp_path):
    """Dry-run means 'report only, nothing written' -- that contract covers
    the review DB too, not just the ingress/library filesystem moves. No DB
    file should even be created."""
    ingress = tmp_path / "ingress"
    library = tmp_path / "library"
    ingress.mkdir()
    library.mkdir()
    _make_truncated_file(ingress)
    review_db = tmp_path / "state" / "review.db"

    report = sweep(ingress, library, live=False, review_db=review_db)

    assert len(report.quarantined) == 1
    assert not review_db.exists()
