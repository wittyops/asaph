import base64
import subprocess
from pathlib import Path

import mutagen
import mutagen.flac
import mutagen.id3
import mutagen.mp3
import pytest

from tagwriter import embed_art, write_tags

JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"


def _make_flac(tmp_path, name="tone.flac") -> Path:
    out = tmp_path / name
    subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "sine=f=440:r=44100:d=2",
         "-c:a", "flac", "-y", str(out)],
        check=True, capture_output=True,
    )
    return out


def _make_mp3(tmp_path, name="tone.mp3") -> Path:
    out = tmp_path / name
    subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "sine=f=440:r=44100:d=2",
         "-c:a", "libmp3lame", "-y", str(out)],
        check=True, capture_output=True,
    )
    return out


# ---- write_tags ----

def test_write_tags_flac_roundtrip(tmp_path):
    f = _make_flac(tmp_path)
    write_tags(f, artist="The Band", album="Great Album", title="Track One")
    audio = mutagen.File(f, easy=True)
    assert audio["artist"] == ["The Band"]
    assert audio["album"] == ["Great Album"]
    assert audio["title"] == ["Track One"]


def test_write_tags_mp3_roundtrip(tmp_path):
    f = _make_mp3(tmp_path)
    write_tags(f, artist="MP3 Artist", album="MP3 Album", title="MP3 Title")
    audio = mutagen.File(f, easy=True)
    assert audio["artist"] == ["MP3 Artist"]
    assert audio["album"] == ["MP3 Album"]
    assert audio["title"] == ["MP3 Title"]


def test_write_tags_only_sets_supplied_fields(tmp_path):
    f = _make_flac(tmp_path)
    write_tags(f, artist="Original Artist", album="Original Album", title="Original Title")
    # Only update title; artist/album must be left untouched.
    write_tags(f, artist=None, album=None, title="New Title")
    audio = mutagen.File(f, easy=True)
    assert audio["artist"] == ["Original Artist"]
    assert audio["album"] == ["Original Album"]
    assert audio["title"] == ["New Title"]


def test_write_tags_raises_on_corrupt_file(tmp_path):
    f = tmp_path / "bad.flac"
    f.write_bytes(b"\x00" * 10)
    with pytest.raises(Exception):
        write_tags(f, artist="X", album="Y", title="Z")


def test_write_tags_raises_on_missing_file(tmp_path):
    f = tmp_path / "does_not_exist.flac"
    with pytest.raises(Exception):
        write_tags(f, artist="X", album="Y", title="Z")


# ---- embed_art ----

def test_embed_art_flac_roundtrip(tmp_path):
    f = _make_flac(tmp_path)
    embed_art(f, JPEG_BYTES, mime="image/jpeg")
    audio = mutagen.flac.FLAC(f)
    pics = audio.pictures
    assert len(pics) == 1
    assert pics[0].data == JPEG_BYTES
    assert pics[0].mime == "image/jpeg"
    assert pics[0].type == 3


def test_embed_art_flac_clears_previous_picture_on_reselect(tmp_path):
    f = _make_flac(tmp_path)
    embed_art(f, JPEG_BYTES, mime="image/jpeg")
    new_bytes = JPEG_BYTES + b"more"
    embed_art(f, new_bytes, mime="image/jpeg")
    audio = mutagen.flac.FLAC(f)
    assert len(audio.pictures) == 1
    assert audio.pictures[0].data == new_bytes


def test_embed_art_mp3_roundtrip(tmp_path):
    f = _make_mp3(tmp_path)
    embed_art(f, JPEG_BYTES, mime="image/jpeg")
    audio = mutagen.mp3.MP3(f)
    apics = audio.tags.getall("APIC")
    assert len(apics) == 1
    assert apics[0].data == JPEG_BYTES
    assert apics[0].mime == "image/jpeg"
    assert apics[0].type == 3


def test_embed_art_mp3_clears_previous_picture_on_reselect(tmp_path):
    f = _make_mp3(tmp_path)
    embed_art(f, JPEG_BYTES, mime="image/jpeg")
    new_bytes = JPEG_BYTES + b"more"
    embed_art(f, new_bytes, mime="image/jpeg")
    audio = mutagen.mp3.MP3(f)
    apics = audio.tags.getall("APIC")
    assert len(apics) == 1
    assert apics[0].data == new_bytes


def _make_ogg(tmp_path, name="tone.ogg") -> Path:
    out = tmp_path / name
    subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "sine=f=440:r=44100:d=2",
         "-c:a", "libvorbis", "-y", str(out)],
        check=True, capture_output=True,
    )
    return out


def _make_opus(tmp_path, name="tone.opus") -> Path:
    out = tmp_path / name
    subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "sine=f=440:r=44100:d=2",
         "-c:a", "libopus", "-y", str(out)],
        check=True, capture_output=True,
    )
    return out


def test_embed_art_ogg_vorbis_roundtrip(tmp_path):
    f = _make_ogg(tmp_path)
    embed_art(f, JPEG_BYTES, mime="image/jpeg")
    audio = mutagen.File(f)
    raw_b64 = audio["metadata_block_picture"][0]
    pic = mutagen.flac.Picture(base64.b64decode(raw_b64))
    assert pic.data == JPEG_BYTES
    assert pic.mime == "image/jpeg"
    assert pic.type == 3


def test_embed_art_opus_roundtrip(tmp_path):
    f = _make_opus(tmp_path)
    embed_art(f, JPEG_BYTES, mime="image/jpeg")
    audio = mutagen.File(f)
    raw_b64 = audio["metadata_block_picture"][0]
    pic = mutagen.flac.Picture(base64.b64decode(raw_b64))
    assert pic.data == JPEG_BYTES
    assert pic.mime == "image/jpeg"
    assert pic.type == 3


def test_embed_art_raises_on_corrupt_file(tmp_path):
    f = tmp_path / "bad.flac"
    f.write_bytes(b"\x00" * 10)
    with pytest.raises(Exception):
        embed_art(f, JPEG_BYTES)


def test_embed_art_raises_on_missing_file(tmp_path):
    f = tmp_path / "does_not_exist.flac"
    with pytest.raises(Exception):
        embed_art(f, JPEG_BYTES)
