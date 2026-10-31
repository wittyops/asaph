import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "lib"))
from flask import Flask

from watchlist import watchlist_bp
from review_queue import REVIEW_DB_KEY
from health_scan import init_review_db


def _make_app(db_path, library_root):
    init_review_db(db_path)
    app = Flask(__name__, template_folder=str(Path(__file__).parent / "templates"))
    app.config[REVIEW_DB_KEY] = db_path
    app.config["SABNZBD_BASE"] = "http://wn-sabnzbd-01:8080"
    app.config["SABNZBD_API_KEY"] = "fakesabkey"
    app.config["SABNZBD_CATEGORY"] = "asaph-staging"
    app.config["LIBRARY_ROOT"] = library_root
    app.register_blueprint(watchlist_bp)
    return app


def _seed_watchlist_entry(db_path, artist="Burial", album=""):
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO watchlist (artist, album, added_at) VALUES (?,?,?)",
        (artist, album, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    entry_id = conn.execute("SELECT id FROM watchlist WHERE artist=? AND album=?", (artist, album)).fetchone()[0]
    conn.close()
    return entry_id


def _seed_candidate(db_path, watchlist_id, title="Burial - Kindred (2012) FLAC",
                     download_url="http://nzb/1", guessed_artist="Burial", guessed_album="Kindred"):
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO watchlist_candidates "
        "(watchlist_id, title, download_url, indexer, size, guessed_artist, guessed_album, found_at, status) "
        "VALUES (?,?,?,?,?,?,?,?,'pending')",
        (watchlist_id, title, download_url, "DOGnzb", 300 * 1048576, guessed_artist, guessed_album,
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    candidate_id = conn.execute(
        "SELECT id FROM watchlist_candidates WHERE download_url=?", (download_url,)
    ).fetchone()[0]
    conn.close()
    return candidate_id


def test_watchlist_page_renders_empty(tmp_path):
    app = _make_app(tmp_path / "review.db", tmp_path / "library")
    resp = app.test_client().get("/watchlist")
    assert resp.status_code == 200
    assert b"Not watching anything yet" in resp.data
    assert b"Nothing new found yet" in resp.data


def test_watchlist_add_creates_entry(tmp_path):
    db_path = tmp_path / "review.db"
    app = _make_app(db_path, tmp_path / "library")
    resp = app.test_client().post("/watchlist/add", data={"artist": "Burial", "album": ""})
    assert resp.status_code == 200
    assert b"Burial" in resp.data
    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT artist, album FROM watchlist").fetchone()
    conn.close()
    assert row == ("Burial", "")


def test_watchlist_add_requires_artist(tmp_path):
    app = _make_app(tmp_path / "review.db", tmp_path / "library")
    resp = app.test_client().post("/watchlist/add", data={"artist": "", "album": "Kindred"})
    assert resp.status_code == 200
    assert b"Artist is required" in resp.data


def test_watchlist_add_duplicate_returns_friendly_message(tmp_path):
    db_path = tmp_path / "review.db"
    app = _make_app(db_path, tmp_path / "library")
    client = app.test_client()
    client.post("/watchlist/add", data={"artist": "Burial", "album": "Kindred"})
    resp = client.post("/watchlist/add", data={"artist": "Burial", "album": "Kindred"})
    assert b"Already on the watchlist" in resp.data
    conn = sqlite3.connect(str(db_path))
    count = conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0]
    conn.close()
    assert count == 1


def test_watchlist_remove_deletes_entry_and_its_candidates(tmp_path):
    db_path = tmp_path / "review.db"
    app = _make_app(db_path, tmp_path / "library")
    entry_id = _seed_watchlist_entry(db_path)
    _seed_candidate(db_path, entry_id)
    resp = app.test_client().post(f"/watchlist/{entry_id}/remove")
    assert resp.status_code == 200
    conn = sqlite3.connect(str(db_path))
    assert conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM watchlist_candidates").fetchone()[0] == 0
    conn.close()


def test_watchlist_dismiss_marks_candidate_dismissed(tmp_path):
    db_path = tmp_path / "review.db"
    app = _make_app(db_path, tmp_path / "library")
    entry_id = _seed_watchlist_entry(db_path)
    candidate_id = _seed_candidate(db_path, entry_id)
    resp = app.test_client().post(f"/watchlist/candidate/{candidate_id}/dismiss")
    assert resp.status_code == 200
    conn = sqlite3.connect(str(db_path))
    status = conn.execute("SELECT status FROM watchlist_candidates WHERE id=?", (candidate_id,)).fetchone()[0]
    conn.close()
    assert status == "dismissed"


@patch("watchlist.grab_release")
def test_watchlist_approve_grabs_and_inserts_review_row(mock_grab, tmp_path):
    mock_grab.return_value = "nzo-123"
    db_path = tmp_path / "review.db"
    library_root = tmp_path / "library"
    app = _make_app(db_path, library_root)
    entry_id = _seed_watchlist_entry(db_path, "Burial", "Kindred")
    candidate_id = _seed_candidate(db_path, entry_id)
    resp = app.test_client().post(f"/watchlist/candidate/{candidate_id}/approve")
    assert resp.status_code == 200
    assert b"badge validated" in resp.data
    assert b"nzo-123" in resp.data

    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT status, artist, album, nzo_id FROM review_items"
    ).fetchone()
    cand_status = conn.execute(
        "SELECT status FROM watchlist_candidates WHERE id=?", (candidate_id,)
    ).fetchone()[0]
    conn.close()
    assert row == ("searching", "Burial", "Kindred", "nzo-123")
    assert cand_status == "approved"


@patch("watchlist.grab_release")
def test_watchlist_approve_already_handled_candidate_returns_error(mock_grab, tmp_path):
    db_path = tmp_path / "review.db"
    app = _make_app(db_path, tmp_path / "library")
    resp = app.test_client().post("/watchlist/candidate/999/approve")
    assert resp.status_code == 200
    assert b"Not found or already handled" in resp.data
    mock_grab.assert_not_called()


@patch("watchlist.grab_release")
def test_watchlist_approve_duplicate_in_progress_returns_friendly_message(mock_grab, tmp_path):
    mock_grab.return_value = "nzo-1"
    db_path = tmp_path / "review.db"
    app = _make_app(db_path, tmp_path / "library")
    entry_id = _seed_watchlist_entry(db_path, "Burial", "Kindred")
    first_candidate = _seed_candidate(db_path, entry_id, download_url="http://nzb/1")
    second_candidate = _seed_candidate(db_path, entry_id, download_url="http://nzb/2")

    first = app.test_client().post(f"/watchlist/candidate/{first_candidate}/approve")
    assert b"badge validated" in first.data

    mock_grab.return_value = "nzo-2"
    second = app.test_client().post(f"/watchlist/candidate/{second_candidate}/approve")
    assert b"already in progress" in second.data
