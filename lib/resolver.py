"""Metadata and art resolution — embedded tags first, then on-demand
AcoustID/MusicBrainz/Cover-Art-Archive API calls. No local mirror, no
standing cache service (spec C1) — every lookup is a direct, timeout-bounded
HTTP call, acceptable because this runs in ephemeral workers, not inline in
Asaph.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import requests

ACOUSTID_API_KEY = os.environ.get("ACOUSTID_API_KEY", "")  # nosemgrep
ACOUSTID_MATCH_THRESHOLD = 0.85
HTTP_TIMEOUT_S = 10


@dataclass
class ResolvedMeta:
    artist: str
    album: str
    title: str
    release_type: str  # album | ep | single | non-album | ambiguous
    source: str         # embedded | acoustid | musicbrainz | ambiguous


def resolve_metadata(tags: dict, fingerprint: str | None) -> ResolvedMeta:
    artist = (tags.get("artist") or "").strip()
    album = (tags.get("album") or "").strip()
    title = (tags.get("title") or "").strip()

    if title and artist:
        release_type = "album" if album else "non-album"
        return ResolvedMeta(artist, album, title, release_type, "embedded")

    if fingerprint:
        # Caller resolves the fingerprint via acoustid_lookup() and
        # musicbrainz_release_type() separately, then calls this again with
        # the embedded-tag shape filled in from those results. This function
        # stays a pure classifier over whatever tags it's given.
        pass

    return ResolvedMeta(artist, album, title, "ambiguous", "ambiguous")


def _acoustid_api_call(fingerprint: str, duration: float) -> dict:
    r = requests.get(
        "https://api.acoustid.org/v2/lookup",
        params={
            "client": ACOUSTID_API_KEY,
            "fingerprint": fingerprint,
            "duration": int(duration),
            "meta": "recordings",
        },
        timeout=HTTP_TIMEOUT_S,
    )
    r.raise_for_status()
    return r.json()


def acoustid_lookup(fingerprint: str, duration: float) -> str | None:
    """Returns a MusicBrainz recording ID on a confident match, else None.
    A miss is a normal, expected outcome for underground/indie material
    (spec C2) — not an error."""
    try:
        data = _acoustid_api_call(fingerprint, duration)
    except Exception:
        return None
    best_score = 0.0
    best_recording_id = None
    for result in data.get("results", []):
        score = result.get("score", 0.0)
        recordings = result.get("recordings", [])
        if score > best_score and recordings:
            best_score = score
            best_recording_id = recordings[0].get("id")
    if best_score >= ACOUSTID_MATCH_THRESHOLD:
        return best_recording_id
    return None


def _cover_art_api_call(mb_release_id: str) -> bytes:
    r = requests.get(
        f"https://coverartarchive.org/release/{mb_release_id}/front",
        timeout=HTTP_TIMEOUT_S,
    )
    r.raise_for_status()
    return r.content


def fetch_cover_art(mb_release_id: str | None) -> bytes | None:
    if not mb_release_id:
        return None
    try:
        return _cover_art_api_call(mb_release_id)
    except Exception:
        return None
