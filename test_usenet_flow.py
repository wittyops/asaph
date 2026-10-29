import subprocess
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).parent))
from usenet_flow import trigger_search, check_completeness, supersede

@patch("usenet_flow.requests.post")
def test_trigger_search_posts_album_search_command(mock_post):
    mock_post.return_value.json.return_value = {"id": 42}
    mock_post.return_value.raise_for_status = lambda: None
    command_id = trigger_search("Bizzle", "Nonstop", "http://lidarr:8686", "fakekey")
    assert command_id == 42
    mock_post.assert_called_once()
    call_kwargs = mock_post.call_args.kwargs
    assert call_kwargs["json"]["name"] == "AlbumSearch"

def _make_valid_flac(path):
    subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
         "-t", "1", "-y", str(path)],
        check=True, capture_output=True,
    )

def test_check_completeness_fails_on_missing_tracks(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    _make_valid_flac(staging / "01.flac")
    assert check_completeness(staging, expected_track_count=3) is False

def test_check_completeness_fails_on_corrupt_track(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    _make_valid_flac(staging / "01.flac")
    (staging / "02.flac").write_bytes(b"\x00" * 10)
    assert check_completeness(staging, expected_track_count=2) is False

def test_check_completeness_passes_when_all_tracks_valid(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    _make_valid_flac(staging / "01.flac")
    _make_valid_flac(staging / "02.flac")
    assert check_completeness(staging, expected_track_count=2) is True

def test_supersede_archives_old_and_promotes_new(tmp_path):
    old_dir = tmp_path / "library" / "Artist" / "Album"
    old_dir.mkdir(parents=True)
    (old_dir / "01.flac").write_bytes(b"old")
    new_dir = tmp_path / "staging" / "Album"
    new_dir.mkdir(parents=True)
    (new_dir / "01.flac").write_bytes(b"new")
    archive_root = tmp_path / "superseded"

    supersede(old_dir, new_dir, archive_root)

    # NOTE: old_dir and promoted are the same path (tmp_path/"library"/
    # "Artist"/"Album") by design of this fixture — supersede() promotes
    # new_dir into old_dir's former location. The brief's original test
    # asserted both `not old_dir.exists()` AND that `promoted` (the same
    # path) has the new content immediately after — self-contradictory.
    # Dropped the `not old_dir.exists()` assertion; the remaining checks
    # (content at that path, archived old content elsewhere) fully cover
    # the documented behavior.
    promoted = tmp_path / "library" / "Artist" / "Album"
    assert (promoted / "01.flac").read_bytes() == b"new"
    archived = list(archive_root.rglob("01.flac"))
    assert len(archived) == 1
    assert archived[0].read_bytes() == b"old"
