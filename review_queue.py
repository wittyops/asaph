"""Flask Blueprint for the curator review queue — the human-in-the-loop
surface for anything the validator/resolver couldn't resolve on its own.
Server-rendered Jinja2 + HTMX; no SPA framework, no build step (spec §5.5).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, current_app, render_template, request
from markupsafe import escape

from lib.resolver import fetch_cover_art
from lib.tagwriter import embed_art, write_tags
from usenet_flow import (
    grab_release,
    pick_best_release,
    sabnzbd_history,
    sabnzbd_queue,
    search_prowlarr,
)

review_bp = Blueprint("review", __name__, template_folder="templates")
REVIEW_DB_KEY = "REVIEW_DB_PATH"


def _db():
    return sqlite3.connect(str(current_app.config[REVIEW_DB_KEY]))


def _categorize(reason: str) -> str:
    """Maps a free-form reason string to a display category used to color-code
    the queue. Reasons come from ingress_sweeper.py and health_scan.py as
    prose, not a fixed enum, so this is substring matching, not a lookup --
    order matters (corrupt beats metadata when a reason mentions both)."""
    r = reason.lower()
    if "ffprobe" in r or "suspect range" in r or "too small" in r or "integrity" in r:
        return "corrupt"
    if "missing" in r or "no embedded art" in r or "unreadable tags" in r or "no album art" in r:
        return "metadata"
    if "searching" in r or "nzo" in r:
        return "usenet"
    return "other"


def _live_usenet_state(nzo_id: str | None, sabnzbd_base: str, api_key: str) -> dict:
    """Best-effort live lookup of an in-flight grab's real state, for the
    detail page only -- never called from the list view, which would mean
    one SABnzbd API call per queue row. This is purely informational: the
    actual resolution logic lives in usenet_poll.py and this never writes
    anything. On any API failure it returns an 'unknown' state so the page
    still renders.

    'searching' on review_items hides at least 4 different real states
    (actively downloading, queued but not yet started, completed and
    waiting for the next poll cycle, or completed-but-stuck because the
    MusicBrainz match was ambiguous) -- this gives the reviewer a real
    answer instead of one static badge for all of them."""
    if not nzo_id:
        return {"icon": "?", "css": "other", "label": "No SABnzbd job recorded for this item."}

    try:
        queue = sabnzbd_queue(sabnzbd_base, api_key)
    except Exception:
        return {"icon": "?", "css": "other", "label": "Could not reach SABnzbd to check status."}

    slot = next((s for s in queue if s.get("nzo_id") == nzo_id), None)
    if slot is not None:
        pct = slot.get("percentage", "?")
        sab_status = slot.get("status", "Downloading")
        return {
            "icon": "⏳",  # hourglass
            "css": "usenet",
            "label": f"{sab_status} — {pct}% complete. Will auto-resolve once finished.",
        }

    try:
        history = sabnzbd_history(sabnzbd_base, api_key)
    except Exception:
        return {"icon": "?", "css": "other", "label": "Could not reach SABnzbd to check status."}

    slot = next((s for s in history if s.get("nzo_id") == nzo_id), None)
    if slot is None:
        return {
            "icon": "⏳",
            "css": "usenet",
            "label": "Queued — waiting for SABnzbd to pick this job up.",
        }

    if slot.get("status") == "Completed":
        return {
            "icon": "✅",  # check mark
            "css": "validated",
            "label": (
                "Download complete — waiting for the next validation cycle to "
                "verify the track count and promote it. If this stays in "
                "'searching' for a long time, the MusicBrainz match was likely "
                "ambiguous and needs a manual relabel + retry."
            ),
        }

    return {
        "icon": "⚠",  # warning
        "css": "corrupt",
        "label": (
            f"SABnzbd reported '{slot.get('status', 'unknown')}'. Will be marked "
            "resolved automatically on the next poll cycle, keeping the existing "
            "library file in place."
        ),
    }


def _row_dict(row: tuple) -> dict:
    item_id, path, reason, status = row[0], row[1], row[2], row[3]
    return {
        "id": item_id,
        "path": path,
        "filename": Path(path).name,
        "reason": reason,
        "status": status,
        "category": _categorize(reason),
        "artist": row[4] if len(row) > 4 else None,
        "album": row[5] if len(row) > 5 else None,
        "nzo_id": row[6] if len(row) > 6 else None,
    }


def _counts(conn):
    row = conn.execute(
        "SELECT "
        "SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN status='searching' THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN status='resolved' AND date(resolved_at)=date('now') THEN 1 ELSE 0 END) "
        "FROM review_items"
    ).fetchone()
    return {
        "quarantined": row[0] or 0,
        "pending_usenet": row[1] or 0,
        "validated_today": row[2] or 0,
    }


@review_bp.route("/review")
def review_page():
    conn = _db()
    rows = conn.execute(
        "SELECT id, path, reason, status, artist, album FROM review_items "
        "WHERE status IN ('pending','searching') ORDER BY flagged_at ASC"
    ).fetchall()
    counts = _counts(conn)
    conn.close()
    items = [_row_dict(r) for r in rows]
    return render_template("review_queue.html", items=items, counts=counts, nav="review")


@review_bp.route("/review/<int:item_id>/approve", methods=["POST"])
def approve(item_id: int):
    conn = _db()
    conn.execute(
        "UPDATE review_items SET status='resolved', resolved_at=? WHERE id=?",
        (datetime.now(timezone.utc).isoformat(), item_id),
    )
    conn.commit()
    conn.close()
    return "", 200


@review_bp.route("/review/<int:item_id>/relabel", methods=["POST"])
def relabel(item_id: int):
    artist = request.form.get("artist", "").strip()
    album = request.form.get("album", "").strip()
    title = request.form.get("title", "").strip()

    conn = _db()
    row = conn.execute("SELECT path FROM review_items WHERE id=?", (item_id,)).fetchone()
    if row is None:
        conn.close()
        return "review item not found", 404
    path = Path(row[0])
    if not path.exists():
        conn.close()
        return "audio file not found on disk", 404

    try:
        write_tags(path, artist or None, album or None, title or None)
    except Exception:
        conn.close()
        return "failed to write tags to audio file", 500

    conn.execute(
        "UPDATE review_items SET status='resolved', resolved_at=?, "
        "reason=reason || ' [relabeled: ' || ? || '/' || ? || '/' || ? || ']' "
        "WHERE id=? AND status != 'resolved'",
        (datetime.now(timezone.utc).isoformat(), artist, album, title, item_id),
    )
    conn.commit()
    conn.close()
    return "", 200


@review_bp.route("/review/<int:item_id>/delete", methods=["POST"])
def delete(item_id: int):
    conn = _db()
    row = conn.execute("SELECT path FROM review_items WHERE id=?", (item_id,)).fetchone()
    if row:
        path = Path(row[0])
        if path.exists():
            path.unlink()
        conn.execute(
            "UPDATE review_items SET status='resolved', resolved_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), item_id),
        )
        conn.commit()
    conn.close()
    return "", 200


@review_bp.route("/review/<int:item_id>")
def detail(item_id: int):
    conn = _db()
    row = conn.execute(
        "SELECT id, path, reason, status, artist, album, nzo_id FROM review_items WHERE id=?",
        (item_id,),
    ).fetchone()
    conn.close()
    item = _row_dict(row) if row else None
    live_state = None
    if item and item["status"] == "searching":
        live_state = _live_usenet_state(
            item["nzo_id"],
            current_app.config["SABNZBD_BASE"],
            current_app.config["SABNZBD_API_KEY"],
        )
    return render_template("track_detail.html", item=item, live_state=live_state, nav="review")


@review_bp.route("/review/<int:item_id>/search-usenet", methods=["POST"])
def search_usenet(item_id: int):
    artist = request.form.get("artist", "").strip()
    album = request.form.get("album", "").strip()

    releases = search_prowlarr(
        artist, album,
        current_app.config["PROWLARR_BASE"],
        current_app.config["PROWLARR_API_KEY"],
    )
    release = pick_best_release(releases)
    if release is None:
        # No results -- a normal, expected outcome, not an error. Don't
        # set status='searching' (there's nothing to poll for), and don't
        # guess at a release. 200, not 404: HTMX only swaps the response
        # into hx-target on a 2xx/3xx, so a 404 here would render nothing
        # and leave the reviewer unsure whether the click even registered.
        return '<p class="result-msg">No usenet release found for that artist/album.</p>', 200

    nzo_id = grab_release(
        release,
        current_app.config["SABNZBD_BASE"],
        current_app.config["SABNZBD_API_KEY"],
        current_app.config["SABNZBD_CATEGORY"],
    )
    conn = _db()
    conn.execute(
        "UPDATE review_items SET status='searching', artist=?, album=?, nzo_id=? WHERE id=?",
        (artist, album, nzo_id, item_id),
    )
    conn.commit()
    conn.close()
    return (
        f'<p class="result-msg">Queued for download (SABnzbd job {escape(nzo_id)}). '
        "This item will move to 'searching' and resolve automatically once it completes.</p>"
    ), 200


@review_bp.route("/review/<int:item_id>/art-candidates", methods=["POST"])
def art_candidates(item_id: int):
    """Renders the manual candidate-entry form. There is no automated
    MusicBrainz release search wired up yet (resolver.py only exposes
    fetch_cover_art(mb_release_id) — a direct Cover Art Archive lookup, not
    a search), so the "candidate picker" is a release-ID entry form that
    posts straight to art-select. Revisit if/when a search endpoint lands
    in lib/resolver.py."""
    return render_template("_art_candidates.html", item_id=item_id)


@review_bp.route("/review/<int:item_id>/art-select", methods=["POST"])
def art_select(item_id: int):
    mb_release_id = request.form.get("mb_release_id", "").strip()
    image_bytes = fetch_cover_art(mb_release_id)
    if image_bytes is None:
        return "no art found for that release", 404

    conn = _db()
    row = conn.execute("SELECT path FROM review_items WHERE id=?", (item_id,)).fetchone()
    if row is None:
        conn.close()
        return "review item not found", 404
    path = Path(row[0])
    if not path.exists():
        conn.close()
        return "audio file not found on disk", 404

    try:
        embed_art(path, image_bytes)
    except Exception:
        conn.close()
        return "failed to embed art in audio file", 500

    conn.execute(
        "UPDATE review_items SET status='resolved', resolved_at=?, "
        "reason = reason || ' [art selected: ' || ? || ']' WHERE id=?",
        (datetime.now(timezone.utc).isoformat(), mb_release_id, item_id),
    )
    conn.commit()
    conn.close()
    return "", 200
