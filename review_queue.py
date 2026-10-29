"""Flask Blueprint for the curator review queue — the human-in-the-loop
surface for anything the validator/resolver couldn't resolve on its own.
Server-rendered Jinja2 + HTMX; no SPA framework, no build step (spec §5.5).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, current_app, render_template, request

from lib.resolver import fetch_cover_art

review_bp = Blueprint("review", __name__, template_folder="templates")
REVIEW_DB_KEY = "REVIEW_DB_PATH"


def _db():
    return sqlite3.connect(str(current_app.config[REVIEW_DB_KEY]))


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
    items = conn.execute(
        "SELECT id, path, reason, status FROM review_items "
        "WHERE status IN ('pending','searching') ORDER BY flagged_at ASC"
    ).fetchall()
    counts = _counts(conn)
    conn.close()
    return render_template("review_queue.html", items=items, counts=counts)


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
        "SELECT id, path, reason, status FROM review_items WHERE id=?", (item_id,)
    ).fetchone()
    conn.close()
    return render_template("track_detail.html", item=row)


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
    conn.execute(
        "UPDATE review_items SET reason = reason || ' [art selected: ' || ? || ']' WHERE id=?",
        (mb_release_id, item_id),
    )
    conn.commit()
    conn.close()
    return "", 200
