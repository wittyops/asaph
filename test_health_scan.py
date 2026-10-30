import sqlite3
import subprocess
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent))
from health_scan import scan_library, init_review_db

def _make_truncated_flac(tmp_path) -> Path:
    good = tmp_path / "good.flac"
    subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
         "-t", "2", "-y", str(good)],
        check=True, capture_output=True,
    )
    bad = tmp_path / "Artist" / "Album" / "01 - Bad.flac"
    bad.parent.mkdir(parents=True)
    # Truncate above validator.MIN_FILE_BYTES (1024) so this exercises the
    # ffmpeg decode-error path in decode_check(), not its separate
    # too-small-to-bother-decoding short-circuit.
    bad.write_bytes(good.read_bytes()[:2000])
    return bad

def test_scan_library_flags_corrupt_file(tmp_path):
    db_path = tmp_path / "curator.db"
    init_review_db(db_path)
    library = tmp_path / "library"
    library.mkdir()
    bad_file = _make_truncated_flac(library)

    flagged_count = scan_library([library], db_path)

    assert flagged_count == 1
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT path, reason, status FROM review_items WHERE path = ?",
        (str(bad_file),),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[2] == "pending"
    assert "decode" in row[1].lower()

def test_scan_library_does_not_flag_clean_files(tmp_path):
    db_path = tmp_path / "curator.db"
    init_review_db(db_path)
    library = tmp_path / "library"
    library.mkdir()
    good = library / "Artist" / "Album"
    good.mkdir(parents=True)
    subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
         "-t", "2", "-y", str(good / "01 - Good.flac")],
        check=True, capture_output=True,
    )
    flagged_count = scan_library([library], db_path)
    assert flagged_count == 0


def test_scan_library_does_not_reverse_a_resolved_human_approval(tmp_path):
    """A human already reviewed and resolved this exact path (e.g. approved
    a false-positive clipping flag). The next weekly scan, finding the same
    file still failing the same heuristic, must leave the row 'resolved' --
    not reset it back to 'pending' and erase the approval."""
    db_path = tmp_path / "curator.db"
    init_review_db(db_path)
    library = tmp_path / "library"
    library.mkdir()
    bad_file = _make_truncated_flac(library)

    import sqlite3 as _sqlite3
    from datetime import datetime, timezone
    conn = _sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO review_items (path, reason, status, flagged_at, resolved_at) "
        "VALUES (?, ?, 'resolved', ?, ?)",
        (str(bad_file), "ffmpeg decode warning: pre-existing, approved by human",
         datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()

    flagged_count = scan_library([library], db_path)

    assert flagged_count == 0  # must not count an already-resolved row as newly flagged
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT status, reason FROM review_items WHERE path = ?", (str(bad_file),)
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "resolved"
    assert "approved by human" in row[1]
