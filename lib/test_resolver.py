from unittest.mock import patch, MagicMock
from resolver import resolve_metadata, acoustid_lookup, fetch_cover_art

def test_resolve_metadata_trusts_embedded_tags_when_complete():
    tags = {"artist": "Bizzle", "album": "Nonstop", "title": "Warriors"}
    result = resolve_metadata(tags, fingerprint=None)
    assert result.artist == "Bizzle"
    assert result.source == "embedded"
    assert result.release_type == "album"

def test_resolve_metadata_non_album_when_no_album_tag():
    tags = {"artist": "Bizzle", "album": "", "title": "Warriors"}
    result = resolve_metadata(tags, fingerprint=None)
    assert result.release_type == "non-album"

def test_resolve_metadata_ambiguous_with_no_tags_and_no_fingerprint():
    result = resolve_metadata({}, fingerprint=None)
    assert result.source == "ambiguous"
    assert result.release_type == "ambiguous"

@patch("resolver._acoustid_api_call")
def test_acoustid_lookup_returns_none_below_threshold(mock_call):
    mock_call.return_value = {"results": [{"score": 0.5, "recordings": [{"id": "abc"}]}]}
    assert acoustid_lookup("fakefp", 180.0) is None

@patch("resolver._acoustid_api_call")
def test_acoustid_lookup_returns_recording_id_above_threshold(mock_call):
    mock_call.return_value = {"results": [{"score": 0.9, "recordings": [{"id": "abc123"}]}]}
    assert acoustid_lookup("fakefp", 180.0) == "abc123"

@patch("resolver._acoustid_api_call")
def test_acoustid_lookup_returns_none_on_api_timeout(mock_call):
    mock_call.side_effect = TimeoutError()
    assert acoustid_lookup("fakefp", 180.0) is None

@patch("resolver._cover_art_api_call")
def test_fetch_cover_art_returns_none_without_release_id(mock_call):
    assert fetch_cover_art(None) is None
    mock_call.assert_not_called()
