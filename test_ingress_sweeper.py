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
