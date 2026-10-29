import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).parent))
from flask import Flask
from review_queue import review_bp, REVIEW_DB_KEY

def _seed_db(db_path, tmp_path):
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE review_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT UNIQUE NOT NULL,
            reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            flagged_at TEXT NOT NULL, resolved_at TEXT
        )
    """)
    f = tmp_path / "bad.flac"
    f.write_bytes(b"\x00" * 10)
    conn.execute(
        "INSERT INTO review_items (path, reason, status, flagged_at) VALUES (?,?,?,?)",
        (str(f), "file too small", "pending", datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    row_id = conn.execute("SELECT id FROM review_items").fetchone()[0]
    conn.close()
    return row_id

def _make_app(db_path):
    app = Flask(__name__, template_folder=str(Path(__file__).parent / "templates"))
    app.config[REVIEW_DB_KEY] = db_path
    app.register_blueprint(review_bp)
    return app

def test_review_page_lists_pending_items(tmp_path):
    db_path = tmp_path / "review.db"
    _seed_db(db_path, tmp_path)
    app = _make_app(db_path)
    client = app.test_client()
    resp = client.get("/review")
    assert resp.status_code == 200
    assert b"file too small" in resp.data

def test_approve_marks_item_resolved(tmp_path):
    db_path = tmp_path / "review.db"
    row_id = _seed_db(db_path, tmp_path)
    app = _make_app(db_path)
    client = app.test_client()
    resp = client.post(f"/review/{row_id}/approve")
    assert resp.status_code == 200
    conn = sqlite3.connect(str(db_path))
    status = conn.execute("SELECT status FROM review_items WHERE id=?", (row_id,)).fetchone()[0]
    conn.close()
    assert status == "resolved"

def test_relabel_is_idempotent_on_double_submit(tmp_path):
    db_path = tmp_path / "review.db"
    row_id = _seed_db(db_path, tmp_path)
    app = _make_app(db_path)
    client = app.test_client()
    form = {"artist": "A", "album": "B", "title": "C"}
    resp1 = client.post(f"/review/{row_id}/relabel", data=form)
    assert resp1.status_code == 200
    resp2 = client.post(f"/review/{row_id}/relabel", data=form)
    assert resp2.status_code == 200
    conn = sqlite3.connect(str(db_path))
    reason = conn.execute("SELECT reason FROM review_items WHERE id=?", (row_id,)).fetchone()[0]
    conn.close()
    assert reason.count("[relabeled:") == 1

def test_delete_removes_file_and_resolves_row(tmp_path):
    db_path = tmp_path / "review.db"
    row_id = _seed_db(db_path, tmp_path)
    app = _make_app(db_path)
    client = app.test_client()
    file_path = Path(sqlite3.connect(str(db_path)).execute(
        "SELECT path FROM review_items WHERE id=?", (row_id,)).fetchone()[0])
    assert file_path.exists()
    resp = client.post(f"/review/{row_id}/delete")
    assert resp.status_code == 200
    assert not file_path.exists()

def test_detail_page_renders_for_existing_item(tmp_path):
    db_path = tmp_path / "review.db"
    row_id = _seed_db(db_path, tmp_path)
    app = _make_app(db_path)
    client = app.test_client()
    resp = client.get(f"/review/{row_id}")
    assert resp.status_code == 200
    assert b"file too small" in resp.data

@patch("review_queue.fetch_cover_art")
def test_art_select_embeds_chosen_art(mock_fetch, tmp_path):
    db_path = tmp_path / "review.db"
    row_id = _seed_db(db_path, tmp_path)
    mock_fetch.return_value = b"\xff\xd8\xfakejpegbytes"
    app = _make_app(db_path)
    client = app.test_client()
    resp = client.post(f"/review/{row_id}/art-select", data={"mb_release_id": "abc-123"})
    assert resp.status_code == 200
    mock_fetch.assert_called_once_with("abc-123")
