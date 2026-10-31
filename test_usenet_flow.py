import subprocess
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).parent))
from usenet_flow import (
    check_completeness,
    grab_release,
    guess_artist_album,
    pick_best_release,
    remap_sabnzbd_path,
    rename_into_staging_layout,
    search_prowlarr,
    supersede,
)


@patch("usenet_flow.requests.get")
def test_search_prowlarr_returns_release_list(mock_get):
    mock_get.return_value.json.return_value = [{"title": "Nonstop", "protocol": "usenet"}]
    mock_get.return_value.raise_for_status = lambda: None
    releases = search_prowlarr("Bizzle", "Nonstop", "http://prowlarr:9696", "fakekey")
    assert releases == [{"title": "Nonstop", "protocol": "usenet"}]
    call_kwargs = mock_get.call_args.kwargs
    assert call_kwargs["params"]["categories"] == 3000
    assert "Bizzle" in call_kwargs["params"]["query"]
    assert call_kwargs["headers"]["X-Api-Key"] == "fakekey"


@patch("usenet_flow.requests.get")
def test_search_prowlarr_query_with_artist_only(mock_get):
    mock_get.return_value.json.return_value = []
    mock_get.return_value.raise_for_status = lambda: None
    search_prowlarr("Bizzle", "", "http://prowlarr:9696", "fakekey")
    assert mock_get.call_args.kwargs["params"]["query"] == "Bizzle"


@patch("usenet_flow.requests.get")
def test_search_prowlarr_query_with_album_only(mock_get):
    mock_get.return_value.json.return_value = []
    mock_get.return_value.raise_for_status = lambda: None
    search_prowlarr("", "Nonstop", "http://prowlarr:9696", "fakekey")
    assert mock_get.call_args.kwargs["params"]["query"] == "Nonstop"


@patch("usenet_flow.requests.get")
def test_search_prowlarr_returns_empty_list_on_no_results(mock_get):
    mock_get.return_value.json.return_value = []
    mock_get.return_value.raise_for_status = lambda: None
    assert search_prowlarr("Nobody", "Nothing", "http://prowlarr:9696", "fakekey") == []


@patch("usenet_flow.requests.get")
def test_search_prowlarr_filters_out_non_usenet_protocol(mock_get):
    # Category 3000 ("Audio") returns torrent-protocol results too (confirmed
    # live against the lab's Prowlarr) -- grab_release() always reads
    # downloadUrl, which torrent results lack, so they must never reach a
    # caller of this function.
    mock_get.return_value.json.return_value = [
        {"title": "torrent result", "protocol": "torrent", "magnetUrl": "magnet:..."},
        {"title": "usenet result", "protocol": "usenet", "downloadUrl": "http://nzb/1"},
    ]
    mock_get.return_value.raise_for_status = lambda: None
    releases = search_prowlarr("Bizzle", "Nonstop", "http://prowlarr:9696", "fakekey")
    assert releases == [{"title": "usenet result", "protocol": "usenet", "downloadUrl": "http://nzb/1"}]


def test_pick_best_release_returns_none_for_empty_list():
    assert pick_best_release([]) is None


def test_pick_best_release_prefers_lossless_category():
    lossy = {"title": "lossy", "categories": [{"id": 3000, "name": "Audio"}]}
    lossless = {"title": "lossless", "categories": [{"id": 3040, "name": "Audio/Lossless"}]}
    result = pick_best_release([lossy, lossless])
    assert result["title"] == "lossless"


def test_pick_best_release_returns_first_when_none_lossless():
    first = {"title": "first", "categories": [{"id": 3000, "name": "Audio"}]}
    second = {"title": "second", "categories": [{"id": 3000, "name": "Audio"}]}
    result = pick_best_release([first, second])
    assert result["title"] == "first"


@patch("usenet_flow.requests.get")
def test_grab_release_returns_nzo_id_on_success(mock_get):
    mock_get.return_value.json.return_value = {"status": True, "nzo_ids": ["nzo-1"]}
    mock_get.return_value.raise_for_status = lambda: None
    release = {"downloadUrl": "http://prowlarr/dl/1"}
    nzo_id = grab_release(release, "http://sabnzbd:8080", "fakekey", "asaph-staging")
    assert nzo_id == "nzo-1"
    call_kwargs = mock_get.call_args.kwargs
    assert call_kwargs["params"]["mode"] == "addurl"
    assert call_kwargs["params"]["name"] == "http://prowlarr/dl/1"
    assert call_kwargs["params"]["cat"] == "asaph-staging"


@patch("usenet_flow.requests.get")
def test_grab_release_raises_on_false_status(mock_get):
    mock_get.return_value.json.return_value = {"status": False, "error": "nope"}
    mock_get.return_value.raise_for_status = lambda: None
    release = {"downloadUrl": "http://prowlarr/dl/1"}
    try:
        grab_release(release, "http://sabnzbd:8080", "fakekey", "asaph-staging")
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass


@patch("usenet_flow.requests.get")
def test_grab_release_raises_on_missing_nzo_ids(mock_get):
    mock_get.return_value.json.return_value = {"status": True}
    mock_get.return_value.raise_for_status = lambda: None
    release = {"downloadUrl": "http://prowlarr/dl/1"}
    try:
        grab_release(release, "http://sabnzbd:8080", "fakekey", "asaph-staging")
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass


def test_remap_sabnzbd_path_strips_prefix_and_joins_staging_root():
    storage = "/data/usenet/complete/asaph-staging/Some Release Folder"
    prefix = Path("/data/usenet/complete/asaph-staging")
    staging_root = Path("/staging")
    result = remap_sabnzbd_path(storage, prefix, staging_root)
    assert result == Path("/staging/Some Release Folder")


def test_remap_sabnzbd_path_raises_on_prefix_mismatch():
    storage = "/some/other/path/Release"
    prefix = Path("/data/usenet/complete/asaph-staging")
    staging_root = Path("/staging")
    try:
        remap_sabnzbd_path(storage, prefix, staging_root)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_rename_into_staging_layout_from_directory(tmp_path):
    sabnzbd_dir = tmp_path / "SABnzbd Named Folder"
    sabnzbd_dir.mkdir()
    (sabnzbd_dir / "01.flac").write_bytes(b"a")
    (sabnzbd_dir / "02.flac").write_bytes(b"b")
    staging_root = tmp_path / "staging"

    result = rename_into_staging_layout(sabnzbd_dir, "AphroDite: Queen", "Greatest Hits", staging_root)

    expected = staging_root / "AphroDite： Queen" / "Greatest Hits"
    assert result == expected
    assert (expected / "01.flac").read_bytes() == b"a"
    assert (expected / "02.flac").read_bytes() == b"b"
    assert not sabnzbd_dir.exists()


def test_rename_into_staging_layout_from_single_file(tmp_path):
    sabnzbd_file = tmp_path / "single_track.flac"
    sabnzbd_file.write_bytes(b"solo")
    staging_root = tmp_path / "staging"

    result = rename_into_staging_layout(sabnzbd_file, "Artist", "Album", staging_root)

    expected_dir = staging_root / "Artist" / "Album"
    assert result == expected_dir
    assert (expected_dir / "single_track.flac").read_bytes() == b"solo"
    assert not sabnzbd_file.exists()


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


def test_guess_artist_album_infers_album_from_known_artist():
    title = "Burial-Kindred-16BIT-44-KHZ-WEB-FLAC-2012-WALKMAN-xpost"
    artist, album = guess_artist_album(title, known_artist="Burial")
    assert artist == "Burial"
    assert album == "Kindred"


def test_guess_artist_album_handles_spaced_dash_and_parenthetical_year():
    title = "Burial - Street Halo Kindred (2012) FLAC"
    artist, album = guess_artist_album(title, known_artist="Burial")
    assert artist == "Burial"
    assert album == "Street Halo Kindred"


def test_guess_artist_album_strips_catalog_number_parenthetical():
    title = "Burial-Street Halo Kindred-(BRC320)-CD-FLAC-2012-SPL"
    artist, album = guess_artist_album(title, known_artist="Burial")
    assert artist == "Burial"
    assert album == "Street Halo Kindred"


def test_guess_artist_album_infers_artist_from_known_album():
    title = "Burial - Street Halo Kindred (2012) FLAC"
    artist, album = guess_artist_album(title, known_album="Street Halo Kindred")
    assert artist == "Burial"
    assert album == "Street Halo Kindred"


def test_guess_artist_album_splits_both_from_dash_when_neither_known():
    title = "Burial - Street Halo Kindred (2012) FLAC"
    artist, album = guess_artist_album(title)
    assert artist == "Burial"
    assert album == "Street Halo Kindred"


def test_guess_artist_album_splits_both_from_bare_dash_scene_name():
    title = "Burial-Kindred-16BIT-44-KHZ-WEB-FLAC-2012-WALKMAN-xpost"
    artist, album = guess_artist_album(title)
    assert artist == "Burial"
    assert album == "Kindred"


def test_guess_artist_album_handles_dotted_scene_release_name():
    title = "Chaka.Khan-Epiphany-The.Best.Of.Chaka.Khan.Vol.1-CD-FLAC-1996-PERFECT-1"
    artist, album = guess_artist_album(title, known_artist="Chaka Khan")
    assert artist == "Chaka Khan"
    assert album == "Epiphany-The Best Of Chaka Khan Vol 1"


def test_guess_artist_album_returns_empty_when_known_artist_not_in_title():
    artist, album = guess_artist_album("Totally Unrelated Title", known_artist="Burial")
    assert artist == "Burial"
    assert album == ""


def test_guess_artist_album_returns_both_empty_with_no_separator_and_nothing_known():
    artist, album = guess_artist_album("NoSeparatorHere")
    assert artist == ""
    assert album == ""


def test_guess_artist_album_passes_through_when_both_already_known():
    artist, album = guess_artist_album(
        "irrelevant title text", known_artist="Burial", known_album="Kindred"
    )
    assert artist == "Burial"
    assert album == "Kindred"
