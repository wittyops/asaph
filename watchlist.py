"""Flask Blueprint for the artist/album watchlist -- "notify me when
something new drops," not "grab it for me automatically." watchlist_poll.py
(run on the same scheduler as the other periodic jobs) is the only thing
that writes watchlist_candidates rows; this blueprint only ever reads them
and, on an explicit human click, performs the actual grab -- the same
grab_release() + review_items insert as acquire.py's manual flow, so an
approved candidate is indistinguishable downstream from a release grabbed
by hand.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, current_app, render_template, request
from markupsafe import escape

from ingress_sweeper import sanitize
from usenet_flow import grab_release

watchlist_bp = Blueprint("watchlist", __name__, template_folder="templates")


def _db():
    return sqlite3.connect(str(current_app.config["REVIEW_DB_PATH"]))


def _load_entries_and_candidates():
    conn = _db()
    entries = conn.execute(
        "SELECT id, artist, album, added_at, last_checked_at FROM watchlist "
        "ORDER BY added_at DESC"
    ).fetchall()
    candidates = conn.execute(
        "SELECT wc.id, wc.title, wc.indexer, wc.size, wc.guessed_artist, "
        "wc.guessed_album, wc.download_url, wc.found_at, w.artist, w.album "
        "FROM watchlist_candidates wc JOIN watchlist w ON w.id = wc.watchlist_id "
        "WHERE wc.status='pending' ORDER BY wc.found_at DESC"
    ).fetchall()
    conn.close()
    return (
        [
            {"id": r[0], "artist": r[1], "album": r[2], "added_at": r[3], "last_checked_at": r[4]}
            for r in entries
        ],
        [
            {
                "id": r[0], "title": r[1], "indexer": r[2], "size": r[3],
                "guess_artist": r[4], "guess_album": r[5], "download_url": r[6],
                "found_at": r[7], "watch_artist": r[8], "watch_album": r[9],
            }
            for r in candidates
        ],
    )


def _render_content():
    entries, candidates = _load_entries_and_candidates()
    return render_template("_watchlist_content.html", entries=entries, candidates=candidates)


@watchlist_bp.route("/watchlist")
def watchlist_page():
    entries, candidates = _load_entries_and_candidates()
    return render_template(
        "watchlist.html", nav="watchlist", entries=entries, candidates=candidates,
    )


@watchlist_bp.route("/watchlist/add", methods=["POST"])
def watchlist_add():
    artist = request.form.get("artist", "").strip()
    album = request.form.get("album", "").strip()
    if not artist:
        return '<p class="result-msg">Artist is required (album is optional -- leave it blank to watch everything by this artist).</p>', 200

    conn = _db()
    try:
        conn.execute(
            "INSERT INTO watchlist (artist, album, added_at) VALUES (?,?,?)",
            (artist, album, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return '<p class="result-msg">Already on the watchlist.</p>', 200
    conn.close()
    return _render_content()


@watchlist_bp.route("/watchlist/<int:entry_id>/remove", methods=["POST"])
def watchlist_remove(entry_id: int):
    conn = _db()
    conn.execute("DELETE FROM watchlist_candidates WHERE watchlist_id=?", (entry_id,))
    conn.execute("DELETE FROM watchlist WHERE id=?", (entry_id,))
    conn.commit()
    conn.close()
    return _render_content()


@watchlist_bp.route("/watchlist/candidate/<int:candidate_id>/dismiss", methods=["POST"])
def watchlist_dismiss(candidate_id: int):
    conn = _db()
    conn.execute(
        "UPDATE watchlist_candidates SET status='dismissed' WHERE id=?",
        (candidate_id,),
    )
    conn.commit()
    conn.close()
    return "", 200


@watchlist_bp.route("/watchlist/candidate/<int:candidate_id>/approve", methods=["POST"])
def watchlist_approve(candidate_id: int):
    conn = _db()
    row = conn.execute(
        "SELECT download_url, guessed_artist, guessed_album FROM watchlist_candidates "
        "WHERE id=? AND status='pending'",
        (candidate_id,),
    ).fetchone()
    if row is None:
        conn.close()
        return '<span class="badge corrupt">Not found or already handled</span>', 200

    download_url, artist, album = row
    artist = (request.form.get("artist") or artist or "").strip()
    album = (request.form.get("album") or album or "").strip()
    if not artist or not album:
        conn.close()
        return '<span class="badge corrupt" title="Missing artist or album">Error</span>', 200

    nzo_id = grab_release(
        {"downloadUrl": download_url},
        current_app.config["SABNZBD_BASE"],
        current_app.config["SABNZBD_API_KEY"],
        current_app.config["SABNZBD_CATEGORY"],
    )

    library_root: Path = current_app.config["LIBRARY_ROOT"]
    album_dir = library_root / sanitize(artist) / sanitize(album)
    placeholder_path = str(album_dir / ".asaph-watchlist")

    try:
        conn.execute(
            "INSERT INTO review_items (path, reason, status, flagged_at, artist, album, nzo_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (placeholder_path, "watchlist auto-find, human-approved grab", "searching",
             datetime.now(timezone.utc).isoformat(), artist, album, nzo_id),
        )
        conn.execute(
            "UPDATE watchlist_candidates SET status='approved' WHERE id=?",
            (candidate_id,),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return (
            '<span class="badge metadata" title="An acquisition for this exact artist/album '
            'is already in progress">Already queued</span>'
        ), 200
    conn.close()

    return (
        f'<a href="/review" class="badge validated" '
        f'title="SABnzbd job {escape(nzo_id)} -- click to track it in the review queue">Queued</a>'
    ), 200
