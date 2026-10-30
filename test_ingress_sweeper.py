import subprocess
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).parent))
from ingress_sweeper import sweep


def _make_truncated_file(tmp_path) -> Path:
    good = tmp_path / "good.flac"
    subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
         "-t", "2", "-y", str(good)],
        check=True, capture_output=True,
    )
    bad = tmp_path / "truncated.flac"
    bad.write_bytes(good.read_bytes()[:200])
    good.unlink()  # only the truncated file should land in ingress/
    return bad


def test_decode_failure_quarantines_as_corrupt(tmp_path):
    ingress = tmp_path / "ingress"
    library = tmp_path / "library"
    ingress.mkdir()
    library.mkdir()
    _make_truncated_file(ingress)

    report = sweep(ingress, library, live=True)

    assert len(report.quarantined) == 1
    assert report.quarantined[0].status == "quarantined-corrupt"


def _make_tagged_flac(path, title="Test Title", artist="Test Artist"):
    subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "2",
         "-metadata", f"title={title}", "-metadata", f"artist={artist}",
         "-metadata", "album=Test Album",
         "-metadata:s:v", "comment=Cover (front)",
         "-y", str(path)],
        check=True, capture_output=True,
    )


def test_clipping_suspect_with_clean_decode_moves_not_quarantines(tmp_path):
    """signal_heuristic_check is a heuristic, documented as 'never a
    pass/fail verdict on its own' -- only decode_check is a real hard gate.
    A file with a clipping suspect but a clean decode must still move into
    the library (status 'moved'), not get hard-quarantined as
    'quarantined-suspect' -- that would leave most normal modern-mastered
    or lossy-decoded audio stuck forever at ingest."""
    ingress = tmp_path / "ingress"
    library = tmp_path / "library"
    ingress.mkdir()
    library.mkdir()
    track = ingress / "track.flac"
    _make_tagged_flac(track)

    with patch("ingress_sweeper.signal_heuristic_check") as mock_suspects, \
         patch("ingress_sweeper.decode_check", return_value=None):
        mock_suspects.return_value = [
            {"start_s": 1.0, "end_s": 2.0, "kind": "clipping"}
        ]
        # Embedded art isn't produced by the ffmpeg metadata flags above, so
        # also patch has_art detection via a sibling cover file instead of
        # fighting mutagen's real art-reading path.
        (ingress / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
        report = sweep(ingress, library, live=True)

    assert len(report.quarantined) == 0
    assert len(report.moved) == 1
    assert report.moved[0].status == "moved"
    assert "clipping" in report.moved[0].reason.lower()
    assert report.moved[0].dest.exists()
