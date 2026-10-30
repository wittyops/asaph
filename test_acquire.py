import sqlite3
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "lib"))
from flask import Flask

from acquire import acquire_bp
from review_queue import REVIEW_DB_KEY
from health_scan import init_review_db


def _make_app(db_path, library_root):
    init_review_db(db_path)
    app = Flask(__name__, template_folder=str(Path(__file__).parent / "templates"))
    app.config[REVIEW_DB_KEY] = db_path
    app.config["PROWLARR_BASE"] = "http://prowlarr:9696"
    app.config["PROWLARR_API_KEY"] = "fakeprowlarrkey"
    app.config["SABNZBD_BASE"] = "http://wn-sabnzbd-01:8080"
    app.config["SABNZBD_API_KEY"] = "fakesabkey"
    app.config["SABNZBD_CATEGORY"] = "asaph-staging"
    app.config["LIBRARY_ROOT"] = library_root
    app.register_blueprint(acquire_bp)
    return app


def test_acquire_page_renders(tmp_path):
    app = _make_app(tmp_path / "review.db", tmp_path / "library")
    resp = app.test_client().get("/acquire")
    assert resp.status_code == 200
    assert b"Search" in resp.data


def test_acquire_search_requires_artist_and_album(tmp_path):
    app = _make_app(tmp_path / "review.db", tmp_path / "library")
    resp = app.test_client().post("/acquire/search", data={"artist": "Bizzle", "album": ""})
    assert resp.status_code == 200
    assert b"both required" in resp.data


@patch("acquire.search_prowlarr")
def test_acquire_search_shows_no_results_message(mock_search, tmp_path):
    mock_search.return_value = []
    app = _make_app(tmp_path / "review.db", tmp_path / "library")
    resp = app.test_client().post("/acquire/search", data={"artist": "Nobody", "album": "Nothing"})
    assert resp.status_code == 200
    assert b"No usenet results" in resp.data


@patch("acquire.search_prowlarr")
def test_acquire_search_renders_results_lossless_first(mock_search, tmp_path):
    mock_search.return_value = [
        {"title": "lossy release", "indexer": "DOGnzb", "size": 100 * 1048576, "age": 10,
         "categories": [{"id": 3000}], "downloadUrl": "http://nzb/lossy"},
        {"title": "lossless release", "indexer": "DOGnzb", "size": 300 * 1048576, "age": 5,
         "categories": [{"id": 3040}], "downloadUrl": "http://nzb/lossless"},
    ]
    app = _make_app(tmp_path / "review.db", tmp_path / "library")
    resp = app.test_client().post("/acquire/search", data={"artist": "Bizzle", "album": "Nonstop"})
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "lossless release" in body
    assert "lossy release" in body
    assert body.index("lossless release") < body.index("lossy release")
    assert 'class="badge validated"' in body


@patch("acquire.grab_release")
def test_acquire_grab_inserts_searching_row_with_sanitized_path(mock_grab, tmp_path):
    mock_grab.return_value = "nzo-999"
    db_path = tmp_path / "review.db"
    library_root = tmp_path / "library"
    app = _make_app(db_path, library_root)
    resp = app.test_client().post(
        "/acquire/grab",
        data={"artist": "Bizzle", "album": "Non:stop", "download_url": "http://nzb/1"},
    )
    assert resp.status_code == 200
    assert b"Queued for download" in resp.data
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT path, status, artist, album, nzo_id FROM review_items"
    ).fetchone()
    conn.close()
    assert row[1] == "searching"
    assert row[2] == "Bizzle"
    assert row[3] == "Non:stop"
    assert row[4] == "nzo-999"
    # Non-breaking colon substitution matches ingress_sweeper.sanitize()'s
    # own library-path convention -- the same one real library files use.
    assert row[0].startswith(str(library_root / "Bizzle"))
    assert ":" not in Path(row[0]).parent.name


@patch("acquire.grab_release")
def test_acquire_grab_missing_fields_does_not_call_grab(mock_grab, tmp_path):
    app = _make_app(tmp_path / "review.db", tmp_path / "library")
    resp = app.test_client().post("/acquire/grab", data={"artist": "Bizzle", "album": ""})
    assert resp.status_code == 200
    assert b"Missing artist, album" in resp.data
    mock_grab.assert_not_called()


@patch("acquire.grab_release")
def test_acquire_grab_duplicate_in_progress_returns_friendly_message(mock_grab, tmp_path):
    mock_grab.return_value = "nzo-1"
    db_path = tmp_path / "review.db"
    library_root = tmp_path / "library"
    app = _make_app(db_path, library_root)
    client = app.test_client()
    first = client.post(
        "/acquire/grab",
        data={"artist": "Bizzle", "album": "Nonstop", "download_url": "http://nzb/1"},
    )
    assert b"Queued for download" in first.data

    mock_grab.return_value = "nzo-2"
    second = client.post(
        "/acquire/grab",
        data={"artist": "Bizzle", "album": "Nonstop", "download_url": "http://nzb/2"},
    )
    assert second.status_code == 200
    assert b"already in progress" in second.data
    conn = sqlite3.connect(str(db_path))
    count = conn.execute("SELECT COUNT(*) FROM review_items").fetchone()[0]
    conn.close()
    assert count == 1
