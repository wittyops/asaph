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


MUSICBRAINZ_USER_AGENT = "Asaph/2.0.0 (wittycomp-lab curator)"


def _musicbrainz_release_search_call(artist: str, album: str) -> dict:
    """Direct MusicBrainz release search (ws/2/release, fmt=json). Each
    result's "media" array already carries a track-count per medium
    (no separate release-detail fetch needed), so this one call is
    enough to get an album's total track count on demand."""
    query = f'artist:"{artist}" AND release:"{album}"'
    r = requests.get(
        "https://musicbrainz.org/ws/2/release/",
        params={"query": query, "fmt": "json"},
        headers={"User-Agent": MUSICBRAINZ_USER_AGENT},
        timeout=HTTP_TIMEOUT_S,
    )
    r.raise_for_status()
    return r.json()


def musicbrainz_track_count(artist: str, album: str) -> int | None:
    """Looks up an album's total track count on demand via MusicBrainz's
    release search (no local lookup table existed before this -- there
    was no pre-existing MusicBrainz track-count call path in this module
    to reuse; this is the first one). Returns None, never zero, when the
    search can't identify a single unambiguous release -- callers must
    treat None as 'leave it alone,' matching the same safety posture as
    acoustid_lookup()'s miss case.

    A genuine API failure (timeout, connection error, non-2xx) is NOT
    caught here -- it propagates to the caller so it can be counted as
    a real error outcome rather than folded into the ambiguous/not-found
    case."""
    data = _musicbrainz_release_search_call(artist, album)
    releases = data.get("releases", [])
    artist_l, album_l = artist.strip().lower(), album.strip().lower()

    matches = []
    for release in releases:
        title = (release.get("title") or "").strip().lower()
        if title != album_l:
            continue
        credits = release.get("artist-credit", [])
        names = {(c.get("name") or "").strip().lower() for c in credits if isinstance(c, dict)}
        if artist_l in names or any(artist_l == n for n in names):
            matches.append(release)

    if len(matches) != 1:
        return None

    media = matches[0].get("media", [])
    if not media:
        return None
    total = sum(m.get("track-count", 0) or 0 for m in media)
    return total or None
