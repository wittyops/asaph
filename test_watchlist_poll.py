import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "lib"))
from health_scan import init_review_db
from watchlist_poll import poll_watchlist


def _seed_watchlist(db_path, artist="Burial", album=""):
    init_review_db(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO watchlist (artist, album, added_at) VALUES (?,?,?)",
        (artist, album, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    entry_id = conn.execute(
        "SELECT id FROM watchlist WHERE artist=? AND album=?", (artist, album)
    ).fetchone()[0]
    conn.close()
    return entry_id


@patch("watchlist_poll.search_prowlarr")
def test_poll_watchlist_surfaces_new_candidate(mock_search, tmp_path):
    mock_search.return_value = [
        {"title": "Burial-Kindred-WEB-FLAC-2012", "downloadUrl": "http://nzb/1",
         "indexer": "DOGnzb", "size": 300 * 1048576},
    ]
    db_path = tmp_path / "review.db"
    _seed_watchlist(db_path, "Burial", "")
    summary = poll_watchlist(db_path, "http://prowlarr:9696", "fakekey")
    assert summary == {"checked": 1, "new_candidates": 1, "errors": 0}

    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT title, download_url, guessed_artist, guessed_album, status FROM watchlist_candidates"
    ).fetchone()
    conn.close()
    assert row[0] == "Burial-Kindred-WEB-FLAC-2012"
    assert row[1] == "http://nzb/1"
    assert row[2] == "Burial"
    assert row[3] == "Kindred"
    assert row[4] == "pending"


@patch("watchlist_poll.search_prowlarr")
def test_poll_watchlist_does_not_duplicate_already_surfaced_candidate(mock_search, tmp_path):
    mock_search.return_value = [
        {"title": "Burial-Kindred-WEB-FLAC-2012", "downloadUrl": "http://nzb/1",
         "indexer": "DOGnzb", "size": 300 * 1048576},
    ]
    db_path = tmp_path / "review.db"
    _seed_watchlist(db_path, "Burial", "")
    poll_watchlist(db_path, "http://prowlarr:9696", "fakekey")
    summary = poll_watchlist(db_path, "http://prowlarr:9696", "fakekey")
    assert summary["new_candidates"] == 0

    conn = sqlite3.connect(str(db_path))
    count = conn.execute("SELECT COUNT(*) FROM watchlist_candidates").fetchone()[0]
    conn.close()
    assert count == 1


@patch("watchlist_poll.search_prowlarr")
def test_poll_watchlist_skips_entry_already_owned_via_review_items(mock_search, tmp_path):
    db_path = tmp_path / "review.db"
    _seed_watchlist(db_path, "Burial", "Kindred")
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO review_items (path, reason, status, flagged_at, artist, album) "
        "VALUES (?,?,?,?,?,?)",
        ("/library/Burial/Kindred/.asaph-acquire", "manual acquisition via /acquire", "resolved",
         datetime.now(timezone.utc).isoformat(), "Burial", "Kindred"),
    )
    conn.commit()
    conn.close()

    summary = poll_watchlist(db_path, "http://prowlarr:9696", "fakekey")
    assert summary == {"checked": 0, "new_candidates": 0, "errors": 0}
    mock_search.assert_not_called()


@patch("watchlist_poll.search_prowlarr")
def test_poll_watchlist_updates_last_checked_at(mock_search, tmp_path):
    mock_search.return_value = []
    db_path = tmp_path / "review.db"
    entry_id = _seed_watchlist(db_path, "Burial", "")
    poll_watchlist(db_path, "http://prowlarr:9696", "fakekey")
    conn = sqlite3.connect(str(db_path))
    last_checked = conn.execute(
        "SELECT last_checked_at FROM watchlist WHERE id=?", (entry_id,)
    ).fetchone()[0]
    conn.close()
    assert last_checked is not None


@patch("watchlist_poll.search_prowlarr")
def test_poll_watchlist_records_error_and_continues(mock_search, tmp_path):
    mock_search.side_effect = Exception("prowlarr unreachable")
    db_path = tmp_path / "review.db"
    _seed_watchlist(db_path, "Burial", "")
    summary = poll_watchlist(db_path, "http://prowlarr:9696", "fakekey")
    assert summary == {"checked": 0, "new_candidates": 0, "errors": 1}


@patch("watchlist_poll.search_prowlarr")
def test_poll_watchlist_ignores_releases_without_download_url(mock_search, tmp_path):
    mock_search.return_value = [{"title": "torrent result", "indexer": "Knaben"}]
    db_path = tmp_path / "review.db"
    _seed_watchlist(db_path, "Burial", "")
    summary = poll_watchlist(db_path, "http://prowlarr:9696", "fakekey")
    assert summary["new_candidates"] == 0
