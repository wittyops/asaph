"""Flask Blueprint for manually searching and grabbing a specific
artist/album via usenet, without going into Sonarr or SABnzbd directly.

Reuses the same Prowlarr search -> SABnzbd grab primitives as
review_queue.py's search-usenet action, but isn't tied to an existing
review_items row -- a manual grab here creates a fresh row so
usenet_poll.py's existing resolution logic (queue/history correlation by
nzo_id, completeness check, promote-into-library) handles it exactly the
same way review-triggered grabs are handled, with one difference: there's
no existing library file to supersede. The row's path is built from the
canonical library layout (ingress_sweeper.py's sanitize(), same as a real
library file would use) so usenet_poll.py's "old_dir doesn't exist" branch
does a plain move into place instead of a supersede+archive.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, current_app, render_template, request
from markupsafe import escape

from ingress_sweeper import sanitize
from usenet_flow import grab_release, guess_artist_album, is_lossless, search_prowlarr

acquire_bp = Blueprint("acquire", __name__, template_folder="templates")


def _db():
    return sqlite3.connect(str(current_app.config["REVIEW_DB_PATH"]))


@acquire_bp.route("/acquire")
def acquire_page():
    return render_template("acquire.html", nav="acquire")


@acquire_bp.route("/acquire/search", methods=["POST"])
def acquire_search():
    artist = request.form.get("artist", "").strip()
    album = request.form.get("album", "").strip()
    if not artist and not album:
        return render_template(
            "_acquire_results.html", results=None, artist=artist, album=album,
            error="Enter an artist, an album, or both.",
        )

    releases = search_prowlarr(
        artist, album,
        current_app.config["PROWLARR_BASE"],
        current_app.config["PROWLARR_API_KEY"],
    )
    # Display order only -- lossless first, then larger files within the
    # same tier (usually the fuller/less-transcoded rip). pick_best_release()'s
    # own auto-pick logic (used by the review queue's one-click grab) is
    # untouched by this.
    releases = sorted(releases, key=lambda r: (not is_lossless(r), -(r.get("size") or 0)))

    # Per-release smart-fill: each result's own title usually pins down the
    # piece the search terms left ambiguous (e.g. an artist-only search
    # still needs an album before grab() can work). Falls back to the raw
    # search terms when the heuristic can't confidently place a field, so
    # this never leaves a row worse off than the old uniform-fill behavior.
    for r in releases:
        guessed_artist, guessed_album = guess_artist_album(r.get("title", ""), artist, album)
        r["guess_artist"] = guessed_artist or artist
        r["guess_album"] = guessed_album or album

    return render_template(
        "_acquire_results.html", results=releases, artist=artist, album=album, error=None,
    )


@acquire_bp.route("/acquire/grab", methods=["POST"])
def acquire_grab():
    artist = request.form.get("artist", "").strip()
    album = request.form.get("album", "").strip()
    download_url = request.form.get("download_url", "").strip()
    if not artist or not album or not download_url:
        return '<span class="badge corrupt" title="Missing artist, album, or download link">Error</span>', 200

    nzo_id = grab_release(
        {"downloadUrl": download_url},
        current_app.config["SABNZBD_BASE"],
        current_app.config["SABNZBD_API_KEY"],
        current_app.config["SABNZBD_CATEGORY"],
    )

    library_root: Path = current_app.config["LIBRARY_ROOT"]
    album_dir = library_root / sanitize(artist) / sanitize(album)
    # Placeholder filename -- this path is never read or written as a real
    # file. Only its parent directory matters: usenet_poll.py uses
    # Path(row.path).parent as the target directory to move the finished
    # download into (or supersede, if something's already there).
    placeholder_path = str(album_dir / ".asaph-acquire")

    conn = _db()
    try:
        conn.execute(
            "INSERT INTO review_items (path, reason, status, flagged_at, artist, album, nzo_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (placeholder_path, "manual acquisition via /acquire", "searching",
             datetime.now(timezone.utc).isoformat(), artist, album, nzo_id),
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
