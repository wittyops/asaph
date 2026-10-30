#!/usr/bin/env python3
"""
ingress-sweeper.py — moves validated, correctly-tagged tracks from
music/ingress/ into music/library/, replacing Beets' role in the pipeline
without MusicBrainz lookups or fetchart network calls (see
docs/superpowers/plans — "Music Ingress Pipeline — Replace the Dead Beets
Mover", 2026-10-29).

Trusts metadata already embedded at download time by yt-dlp
(FFmpegMetadata + EmbedThumbnail, both MeTube's metube-audio and the
phone-side dlnis.sh apply these). Does not transcode, does not fetch
metadata from any network source, does not run MusicBrainz matching.

Usage:
    python3 ingress-sweeper.py [--live] [--ingress PATH] [--library PATH]
        --live          Actually move files and notify. Default is dry-run:
                         report only, nothing written, nothing moved.
        --ingress PATH  Default /ingress (container mount)
        --library PATH  Default /library (container mount)

Exit code is always 0 on a clean run (even with quarantined items) — a
quarantine is expected, routine output, not a script failure. Non-zero only
on an actual crash (unreadable mount, etc).
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

try:
    from mutagen import File as MutagenFile
except ImportError:
    print("FATAL: mutagen not installed — this script must run inside the "
          "wn-ingress-sweeper image (see Dockerfile.ingress-sweeper), not "
          "bare python3 on the host.", file=sys.stderr)
    sys.exit(1)

from lib.validator import decode_check, signal_heuristic_check

AUDIO_EXTS = {".flac", ".opus", ".m4a", ".mp3", ".ogg", ".oga", ".wav"}
# Non-audio files the sweeper deliberately leaves alone — never quarantined,
# never reported as an error, just not its concern.
IGNORE_EXTS = {".m3u", ".m3u8", ".description", ".webp", ".json", ".info.json",
               ".jpg", ".jpeg", ".png", ".txt", ".nomedia"}
IGNORE_DIR_NAMES = {".metube", ".stfolder", ".stversions", ".thumbnails",
                     "audio-yt-full", "quarantine"}

# Same full-width-colon substitution already present throughout the existing
# library (yt-dlp's own default sanitization) — match it, don't invent a
# second escaping scheme.
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_COLON_SUB = "："


def sanitize(name: str) -> str:
    name = unicodedata.normalize("NFC", name).strip()
    name = name.replace(":", _COLON_SUB)
    name = _UNSAFE.sub("_", name)
    name = name.rstrip(". ")
    return name or "Unknown"


@dataclass
class Item:
    src: Path
    status: str = ""          # moved | quarantined | skipped-dupe | error
    reason: str = ""
    dest: Path | None = None


@dataclass
class Report:
    moved: list = field(default_factory=list)
    quarantined: list = field(default_factory=list)
    skipped_dupe: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    left_alone_count: int = 0

    def add(self, item: Item):
        bucket = {
            "moved": self.moved,
            "quarantined": self.quarantined,
            "quarantined-corrupt": self.quarantined,
            "quarantined-suspect": self.quarantined,
            "skipped-dupe": self.skipped_dupe,
            "error": self.errors,
        }[item.status]
        bucket.append(item)

    def summary(self) -> str:
        return (f"moved={len(self.moved)} quarantined={len(self.quarantined)} "
                f"skipped_dupe={len(self.skipped_dupe)} errors={len(self.errors)} "
                f"left_alone={self.left_alone_count}")


def read_tags(path: Path) -> dict:
    audio = MutagenFile(path)
    if audio is None:
        return {}
    tags = {}
    tags["duration"] = getattr(audio.info, "length", None) if audio.info else None

    def first(*keys):
        if not audio.tags:
            return None
        for k in keys:
            # mutagen's Vorbis-comment dict (FLAC/OPUS/OGG) raises ValueError
            # (not KeyError) for a key that isn't valid in that container's
            # tag scheme — e.g. probing an MP4 atom name ("\xa9alb") against
            # a FLAC/OPUS file. .get()'s own default doesn't catch that, so
            # this has to.
            try:
                v = audio.tags.get(k)
            except (KeyError, ValueError):
                continue
            if v:
                return str(v[0]) if isinstance(v, list) else str(v)
        return None

    if hasattr(audio, "tags") and audio.tags is not None:
        # Vorbis comments (FLAC/OPUS/OGG) use plain keys; MP4 uses atom codes;
        # ID3 uses frame IDs. Cover the common shapes without a full mapping
        # table — yt-dlp's FFmpegMetadata writes Vorbis-style keys into every
        # container it touches via ffmpeg, so this covers the real cases.
        tags["artist"] = first("artist", "ARTIST", "\xa9ART", "TPE1")
        tags["albumartist"] = first("albumartist", "ALBUMARTIST", "aART", "TPE2") or tags["artist"]
        tags["album"] = first("album", "ALBUM", "\xa9alb", "TALB")
        tags["title"] = first("title", "TITLE", "\xa9nam", "TIT2")
        tags["track"] = first("tracknumber", "TRACKNUMBER", "trkn", "TRCK")

    tags["has_art"] = _has_embedded_art(audio)
    return tags


def _has_embedded_art(audio) -> bool:
    if audio is None:
        return False
    # FLAC/OGG: .pictures ; MP4: 'covr' atom ; ID3: any APIC frame
    if hasattr(audio, "pictures") and audio.pictures:
        return True
    if audio.tags:
        if "covr" in audio.tags:
            return True
        if any(k.startswith("APIC") for k in audio.tags.keys()):
            return True
    return False


def probe_duration(path: Path) -> float | None:
    try:
        audio = MutagenFile(path)
        return getattr(audio.info, "length", None) if audio and audio.info else None
    except Exception:
        return None


def move_file(src: Path, dest: Path) -> None:
    """os.replace requires same filesystem; ingress/ and library/ may be
    separate bind mounts even under a shared host parent. Fall back to
    copy+remove (not shutil.move, which silently does the same thing less
    explicitly) so a cross-device library layout works the same as a
    same-device one."""
    try:
        os.replace(src, dest)
    except OSError as e:
        if e.errno != 18:  # EXDEV — anything else is a real error, re-raise
            raise
        shutil.copy2(src, dest)
        os.remove(src)


def find_sibling_art(path: Path) -> Path | None:
    for name in ("cover.jpg", "cover.png", "folder.jpg", "folder.png"):
        candidate = path.parent / name
        if candidate.is_file():
            return candidate
    return None


def canonical_dest(library_root: Path, tags: dict, ext: str) -> Path:
    artist = sanitize(tags.get("albumartist") or "Unknown Artist")
    album = tags.get("album")
    title = sanitize(tags.get("title") or "Unknown Title")
    track = tags.get("track")

    if album and album.strip() and album.strip().upper() != "NA":
        album_dir = sanitize(album)
        rel_dir = Path(artist) / album_dir
    else:
        rel_dir = Path(artist) / "Non-Album" / artist

    track_num_str = ""
    if track:
        m = re.match(r"\s*(\d+)", track)
        if m:
            track_num_str = f"{int(m.group(1)):02d} - "

    filename = f"{track_num_str}{title}{ext}"
    return library_root / rel_dir / filename


def existing_dupe(dest: Path, src_duration: float | None) -> Path | None:
    if dest.exists():
        return dest
    if src_duration is None:
        return None
    # Same artist+title dir, different extension/track-number formatting —
    # check siblings by duration (±2s) before declaring a dupe.
    if not dest.parent.is_dir():
        return None
    for sibling in dest.parent.iterdir():
        if sibling.suffix.lower() not in AUDIO_EXTS:
            continue
        dur = probe_duration(sibling)
        if dur is not None and abs(dur - src_duration) <= 2.0:
            return sibling
    return None


def walk_ingress(ingress_root: Path):
    for dirpath, dirnames, filenames in os.walk(ingress_root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIR_NAMES and not d.startswith(".")]
        for fname in filenames:
            p = Path(dirpath) / fname
            if p.suffix.lower() in AUDIO_EXTS:
                yield p


def sweep(ingress_root: Path, library_root: Path, live: bool) -> Report:
    report = Report()
    audio_files = list(walk_ingress(ingress_root))

    for path in audio_files:
        # Decode/signal checks run before read_tags(): a corrupt/truncated
        # container can throw inside mutagen's own parser (e.g. a PICTURE or
        # VORBIS_COMMENT block that claims more bytes than the file has left)
        # before any tag is ever reached. Checking decodability first means
        # that class of file is correctly quarantined as "corrupt" instead of
        # falling into the generic "error" bucket below.
        decode_err = decode_check(path)
        if decode_err:
            report.add(Item(path, "quarantined-corrupt", decode_err))
            continue

        # signal_heuristic_check is documented (lib/validator.py) as a
        # heuristic -- "never a pass/fail verdict on its own" -- and the
        # spec routes its suspects to human review, not a hard ingest
        # block. decode_check above is the only real pass/fail gate here:
        # a file that fails to decode is genuinely broken. A clipping
        # suspect alone (which the -0.1 dBFS threshold will flag on a
        # large share of normal modern-mastered/lossy-decoded audio) must
        # not block the move -- it's recorded for visibility, but the file
        # still proceeds exactly like a clean one.
        suspects = signal_heuristic_check(path)
        suspect_note = ""
        if suspects:
            suspect_note = f"{len(suspects)} suspect range(s): " + \
                "; ".join(f"{s['kind']} at {s['start_s']:.1f}-{s['end_s']:.1f}s" for s in suspects)

        try:
            tags = read_tags(path)
        except Exception as e:
            report.add(Item(path, "error", f"unreadable tags: {e}"))
            continue

        if not tags or not tags.get("title") or not (tags.get("albumartist") or tags.get("artist")):
            report.add(Item(path, "quarantined", "missing artist/title tags"))
            continue

        has_art = tags.get("has_art") or find_sibling_art(path) is not None
        if not has_art:
            report.add(Item(path, "quarantined", "no embedded art, no sibling cover.jpg/folder.jpg"))
            continue

        dest = canonical_dest(library_root, tags, path.suffix.lower())
        dupe = existing_dupe(dest, tags.get("duration"))
        if dupe is not None:
            report.add(Item(path, "skipped-dupe", f"matches existing {dupe}", dest=dupe))
            continue

        reason = "validated, moved to canonical path"
        if suspect_note:
            reason += f" [signal heuristic note, not a block: {suspect_note}]"
        item = Item(path, "moved", reason, dest=dest)
        if live:
            dest.parent.mkdir(parents=True, exist_ok=True)
            move_file(path, dest)
            sibling_art = find_sibling_art(path)
            if sibling_art and not tags.get("has_art") and sibling_art.exists():
                art_dest = dest.parent / sibling_art.name
                if not art_dest.exists():
                    try:
                        shutil.copy2(sibling_art, art_dest)
                    except Exception:
                        pass
        report.add(item)

    # Count everything under ingress_root that isn't an audio file and isn't
    # in an ignored dir — purely informational, never acted on.
    for _dirpath, dirnames, filenames in os.walk(ingress_root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIR_NAMES and not d.startswith(".")]
        for fname in filenames:
            if Path(fname).suffix.lower() not in AUDIO_EXTS:
                report.left_alone_count += 1

    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--ingress", default="/ingress")
    ap.add_argument("--library", default="/library")
    args = ap.parse_args()

    ingress_root = Path(args.ingress)
    library_root = Path(args.library)

    if not ingress_root.is_dir():
        print(f"FATAL: ingress root {ingress_root} not found", file=sys.stderr)
        sys.exit(1)
    if not library_root.is_dir():
        print(f"FATAL: library root {library_root} not found", file=sys.stderr)
        sys.exit(1)

    report = sweep(ingress_root, library_root, live=args.live)

    mode = "LIVE" if args.live else "DRY-RUN"
    print(f"=== ingress-sweeper {mode} === {report.summary()}")
    print()
    if report.moved:
        print(f"-- moved ({len(report.moved)}) --")
        for it in report.moved:
            print(f"  {it.src}  ->  {it.dest}")
        print()
    if report.quarantined:
        print(f"-- quarantined ({len(report.quarantined)}) -- left in place in ingress/")
        for it in report.quarantined:
            print(f"  {it.src}  :: {it.reason}")
        print()
    if report.skipped_dupe:
        print(f"-- skipped as duplicate ({len(report.skipped_dupe)}) -- left in place in ingress/")
        for it in report.skipped_dupe:
            print(f"  {it.src}  :: {it.reason}")
        print()
    if report.errors:
        print(f"-- errors ({len(report.errors)}) --")
        for it in report.errors:
            print(f"  {it.src}  :: {it.reason}")
        print()
    print(f"-- left alone (non-audio files, not this script's concern): {report.left_alone_count} --")

    sys.exit(0)


if __name__ == "__main__":
    main()
