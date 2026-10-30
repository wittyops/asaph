"""Writing tags and embedded cover art into audio files on disk.

This is the third module in the lib/ trio: validator.py checks files,
resolver.py fetches metadata/art over HTTP, tagwriter.py writes what
resolver.py found back into the actual file. Nothing upstream of this
module touches files on disk directly -- callers (review_queue.py) only
get to mark a review item resolved after a write here has actually
succeeded.
"""
from __future__ import annotations

import base64
from pathlib import Path

import mutagen
import mutagen.flac
import mutagen.id3
import mutagen.mp4
import mutagen.oggopus
import mutagen.oggvorbis


def write_tags(
    path: Path,
    artist: str | None,
    album: str | None,
    title: str | None,
) -> None:
    """Writes artist/album/title tags into the file at `path` using
    mutagen's format-agnostic "easy" interface (normalizes these three
    keys across FLAC/OGG/OPUS/MP3/MP4 -- no per-format branching needed
    for plain text tags).

    Only fields the caller actually supplied (non-empty strings) are
    written; a field left as None is left untouched on disk, not blanked
    out -- the review form may legitimately submit only some fields.

    Raises on anything that prevents a real write (unrecognized/corrupt
    file, mutagen error) -- this must never silently no-op, since the
    caller uses success/failure here to decide whether to mark a review
    item resolved.
    """
    audio = mutagen.File(path, easy=True)
    if audio is None:
        raise ValueError(f"unrecognized or corrupt audio file: {path}")

    if artist:
        audio["artist"] = artist
    if album:
        audio["album"] = album
    if title:
        audio["title"] = title

    audio.save()


def _front_cover_picture(image_bytes: bytes, mime: str) -> "mutagen.flac.Picture":
    pic = mutagen.flac.Picture()
    pic.type = 3  # front cover
    pic.mime = mime
    pic.data = image_bytes
    return pic


def embed_art(path: Path, image_bytes: bytes, mime: str = "image/jpeg") -> None:
    """Embeds `image_bytes` as the front-cover picture of the file at
    `path`. Each container family has its own picture-embedding API in
    mutagen, so this dispatches on the real (non-easy) tag object rather
    than using the easy interface, which has no picture support at all.

    Raises on an unrecognized format or any mutagen failure -- same
    never-silently-no-op contract as write_tags().
    """
    audio = mutagen.File(path)
    if audio is None:
        raise ValueError(f"unrecognized or corrupt audio file: {path}")

    if isinstance(audio, mutagen.flac.FLAC):
        audio.clear_pictures()
        audio.add_picture(_front_cover_picture(image_bytes, mime))
        audio.save()
        return

    if isinstance(audio, mutagen.mp4.MP4):
        fmt = (
            mutagen.mp4.MP4Cover.FORMAT_JPEG
            if "jpeg" in mime or "jpg" in mime
            else mutagen.mp4.MP4Cover.FORMAT_PNG
        )
        audio["covr"] = [mutagen.mp4.MP4Cover(image_bytes, imageformat=fmt)]
        audio.save()
        return

    if audio.tags is not None and isinstance(audio.tags, mutagen.id3.ID3):
        id3 = audio.tags
        id3.delall("APIC")
        id3.add(
            mutagen.id3.APIC(
                encoding=3,
                mime=mime,
                type=3,
                desc="Cover",
                data=image_bytes,
            )
        )
        audio.save()
        return

    # MP3 files opened via mutagen.File() may come back as an MP3 object
    # whose .tags is an ID3 instance -- already covered above via the
    # isinstance check since MP3's ID3 attaches as `.tags`. For a bare
    # ID3-only file (no recognized audio stream), mutagen.File() itself
    # would have returned an ID3 object directly.
    if isinstance(audio, mutagen.id3.ID3):
        audio.delall("APIC")
        audio.add(
            mutagen.id3.APIC(
                encoding=3,
                mime=mime,
                type=3,
                desc="Cover",
                data=image_bytes,
            )
        )
        audio.save()
        return

    # Vorbis-comment containers that are NOT FLAC (i.e. OGG/OPUS): picture
    # data goes in as a base64-encoded METADATA_BLOCK_PICTURE value built
    # from a FLAC Picture object's own binary serialization. This is
    # mutagen's documented pattern for embedding art in Vorbis comments.
    if isinstance(audio, (mutagen.oggvorbis.OggVorbis, mutagen.oggopus.OggOpus)):
        pic = _front_cover_picture(image_bytes, mime)
        encoded = base64.b64encode(pic.write()).decode("ascii")
        audio["metadata_block_picture"] = [encoded]
        audio.save()
        return

    raise ValueError(f"unsupported audio format for art embedding: {path} ({type(audio)})")
