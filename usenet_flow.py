"""Usenet acquisition trigger + staging-import validation + supersede.

Talks directly to Prowlarr (search) and SABnzbd (grab) -- the same
underlying usenet stack Lidarr itself relies on -- and owns the whole
search -> grab -> validate -> supersede flow itself. Lidarr is NOT part
of this flow.

Why Lidarr was dropped (live-verified against wn-lidarr-01,
10.10.30.58:8686, 2026-10-30): Lidarr enforces exactly one artist record
per MusicBrainz ID globally (ArtistExistsValidator rejects a duplicate),
so there is no way to give it a disposable "staging" identity for an
artist without touching its real monitored identity. Its only root
folder is the live library itself, so any search it completes today
imports straight into the live library with zero gate. And Lidarr only
knows 279 artists total -- the old AlbumSearch-command approach had no
real effect for the (likely large) share of corrupt-file artists Lidarr
has never indexed. None of these constraints apply to the Prowlarr+
SABnzbd design below, because Lidarr is no longer in the loop at all."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import requests

from ingress_sweeper import sanitize
from lib.validator import decode_check

AUDIO_EXTENSIONS = {".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".aac"}
HTTP_TIMEOUT_S = 15
LOSSLESS_CATEGORY_ID = 3040

# Scene-release naming has a handful of common conventions ("Artist - Album
# (Year) Format", "Artist-Album-Quality-Format-Year-Group") but no fixed
# grammar, so this is a heuristic boundary marker, not a parser: anything
# from the first match onward is quality/format/year/release-group noise,
# never part of the artist or album.
_RELEASE_NOISE_MARKERS = re.compile(
    r"\b(FLAC|MP3|ALAC|WAV|APE|OGG|OPUS|CD|WEB|VINYL|DVD|SACD|NMR|DELUXE|"
    r"REMASTER(?:ED)?|BOOTLEG|PROPER|REPACK|RETAIL|LIMITED|EDITION|"
    r"\d{2,4}KBPS|\d{2,3}KHZ|\d{1,2}BIT|19\d{2}|20\d{2})\b",
    re.IGNORECASE,
)


def _truncate_at_release_noise(text: str) -> str:
    """Cuts a title fragment at the first format/year/catalog-number marker
    (an opening paren counts too -- that's almost always a year or a
    catalog number like "(BRC320)", never album text)."""
    cut = len(text)
    paren_idx = text.find("(")
    if paren_idx != -1:
        cut = min(cut, paren_idx)
    m = _RELEASE_NOISE_MARKERS.search(text)
    if m:
        cut = min(cut, m.start())
    return text[:cut].strip(" -:").strip()


def guess_artist_album(title: str, known_artist: str = "", known_album: str = "") -> tuple[str, str]:
    """Best-effort split of a scene-style release title into (artist, album),
    for prefilling the acquire page's per-row fields so a reviewer doesn't
    have to type both by hand for every result. Not a parser -- scene
    release names don't follow one fixed grammar -- so this is a heuristic
    that degrades to ('', '') on anything it can't confidently place.
    Callers must treat an empty string as 'leave it for the user to fill
    in,' never as a value safe to grab on.

    - If the caller already knows the artist (typed it into the search),
      that string is trusted over any guess and only the album is inferred
      by locating the artist in the title and reading the field after it.
    - Symmetric for a known album.
    - If neither is known, the title is split on the first "Artist - Album"
      or "Artist-Album" separator."""
    clean = re.sub(r"\s+", " ", title.replace(".", " ")).strip()
    artist = known_artist.strip()
    album = known_album.strip()

    if artist and not album:
        idx = clean.lower().find(artist.lower())
        if idx != -1:
            rest = clean[idx + len(artist):].lstrip(" -:")
            album = _truncate_at_release_noise(rest)
    elif album and not artist:
        idx = clean.lower().find(album.lower())
        if idx != -1:
            artist = clean[:idx].rstrip(" -:")
    elif not artist and not album:
        if " - " in clean:
            left, _, rest = clean.partition(" - ")
        elif "-" in clean:
            left, _, rest = clean.partition("-")
        else:
            return "", ""
        artist = left.strip()
        album = _truncate_at_release_noise(rest.strip())

    return artist, album


def search_prowlarr(artist: str, album: str, prowlarr_base: str, api_key: str) -> list[dict]:
    """Searches Prowlarr for releases matching artist and/or album -- either
    may be blank (the manual /acquire search allows searching by just one),
    but not both; callers are responsible for rejecting that case before
    calling this, since an empty query string would just return whatever
    Prowlarr does with no search term. Returns the raw list of release
    dicts (empty list if none -- a miss is a normal, expected outcome, not
    an error).

    Category 3000 ("Audio") covers every indexer tagged for audio content
    regardless of protocol, including torrent-only indexers (confirmed
    live 2026-10-30: a search against the lab's real Prowlarr instance
    returned a torrent-protocol Knaben result alongside the usenet ones).
    grab_release() always reads release["downloadUrl"], which only
    torrent-protocol results lack -- so this whole pipeline is usenet-only
    by design (see the module docstring on why Lidarr was dropped), and a
    torrent result reaching grab_release() would KeyError. Filtered here,
    once, so every caller (the review queue's auto-grab and the manual
    /acquire search) gets a safe, usenet-only list."""
    query = " ".join(part for part in (artist, album) if part)
    resp = requests.get(
        f"{prowlarr_base}/api/v1/search",
        headers={"X-Api-Key": api_key},
        params={"query": query, "categories": 3000},
        timeout=HTTP_TIMEOUT_S,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        return []
    return [r for r in data if r.get("protocol") == "usenet"]


def is_lossless(release: dict) -> bool:
    for cat in release.get("categories", []) or []:
        if cat.get("id") == LOSSLESS_CATEGORY_ID:
            return True
    return False


def pick_best_release(releases: list[dict]) -> dict | None:
    """Prefers a release whose categories include the lossless-audio id
    (3040) over any that don't; among equally-ranked candidates, returns
    the first. Returns None for an empty list -- callers must treat that
    as a 'no results' outcome, never guessing a release."""
    if not releases:
        return None

    for release in releases:
        if is_lossless(release):
            return release
    return releases[0]


def grab_release(release: dict, sabnzbd_base: str, api_key: str, category: str) -> str:
    """Hands a release's downloadUrl straight to SABnzbd's addurl API
    (the URL is already a ready-to-fetch, Prowlarr-proxied/authenticated
    NZB link -- do not append or modify it). Returns the resulting
    nzo_id. Raises on any failure (bad HTTP status, non-true `status` in
    the response body, or a missing nzo_ids list) -- the caller must
    never proceed with a grab it can't confirm actually queued."""
    download_url = release["downloadUrl"]
    resp = requests.get(
        f"{sabnzbd_base}/api",
        params={
            "mode": "addurl",
            "name": download_url,
            "cat": category,
            "apikey": api_key,
            "output": "json",
        },
        timeout=HTTP_TIMEOUT_S,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("status"):
        raise RuntimeError(f"SABnzbd addurl failed: {data}")
    nzo_ids = data.get("nzo_ids")
    if not nzo_ids:
        raise RuntimeError(f"SABnzbd addurl returned no nzo_ids: {data}")
    return nzo_ids[0]


def sabnzbd_queue(sabnzbd_base: str, api_key: str) -> list[dict]:
    """Shared by usenet_poll.py (resolution) and review_queue.py (display) --
    both need the live SABnzbd queue, keyed by nzo_id, and there's no reason
    for two copies of this GET."""
    resp = requests.get(
        f"{sabnzbd_base}/api",
        params={"mode": "queue", "apikey": api_key, "output": "json"},
        timeout=HTTP_TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp.json().get("queue", {}).get("slots", [])


def sabnzbd_history(sabnzbd_base: str, api_key: str, limit: int = 50) -> list[dict]:
    """Shared by usenet_poll.py (resolution) and review_queue.py (display)."""
    resp = requests.get(
        f"{sabnzbd_base}/api",
        params={"mode": "history", "apikey": api_key, "output": "json", "limit": limit},
        timeout=HTTP_TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp.json().get("history", {}).get("slots", [])


def remap_sabnzbd_path(storage: str, sabnzbd_staging_prefix: Path, staging_root: Path) -> Path:
    """Translates a SABnzbd-reported `storage` path (SABnzbd's own
    container-side view, e.g. /data/usenet/complete/asaph-staging/<dir>)
    into the path Asaph itself can open, via its own mount of the same
    underlying host directory at STAGING_ROOT (e.g. /staging). Raises if
    `storage` doesn't actually start with sabnzbd_staging_prefix -- that
    would mean the category config drifted and this code must not
    guess."""
    storage_path = Path(storage)
    relative = storage_path.relative_to(sabnzbd_staging_prefix)
    return staging_root / relative


def rename_into_staging_layout(sabnzbd_dir: Path, artist: str, album: str, staging_root: Path) -> Path:
    """Moves/renames the arbitrary SABnzbd-named folder (or single file)
    into the canonical staging_root/sanitize(artist)/sanitize(album)
    layout, reusing ingress_sweeper.py's sanitize() rather than a second
    escaping scheme. Handles both a directory grab and a single-file
    (non-RAR, single-NZB) grab. Returns the new path."""
    dest_dir = staging_root / sanitize(artist) / sanitize(album)
    dest_dir.mkdir(parents=True, exist_ok=True)

    if sabnzbd_dir.is_file():
        dest = dest_dir / sabnzbd_dir.name
        shutil.move(str(sabnzbd_dir), str(dest))
        return dest_dir

    for child in sabnzbd_dir.iterdir():
        shutil.move(str(child), str(dest_dir / child.name))
    if sabnzbd_dir.exists():
        sabnzbd_dir.rmdir()
    return dest_dir


def check_completeness(staging_dir: Path, expected_track_count: int) -> bool:
    """True only if staging_dir contains exactly expected_track_count audio
    files and every one of them passes decode_check. A partial release
    (e.g. disc 1 of 2 posted) must fail this, not pass because the grab
    reported 'complete.'"""
    tracks = [
        p for p in staging_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
    ]
    if len(tracks) != expected_track_count:
        return False
    for track in tracks:
        if decode_check(track) is not None:
            return False
    return True


def supersede(old_dir: Path, new_dir: Path, archive_root: Path) -> None:
    """Archives old_dir under archive_root (reversible — never a hard
    delete), then moves new_dir to take old_dir's former place."""
    archive_dest = archive_root / old_dir.relative_to(old_dir.anchor)
    archive_dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(old_dir), str(archive_dest))
    old_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(new_dir), str(old_dir))
