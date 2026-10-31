# Changelog — Asaph

All notable changes to Asaph (the Wittycomp Lab music curator) are tracked here.

## [2.0.0] — 2026-10-29, live/deployed, tagged 2026-10-31

Major version jump marking Asaph's expansion from a passive health-checker into
a full audio ingress pipeline: shared validator (decode-check + signal-heuristic
+ checksum-verify), extended ingress-sweeper with metadata/art resolution,
a scheduled library health scan, a direct Prowlarr+SABnzbd usenet acquisition +
supersede flow (**not** Lidarr — see `usenet_flow.py`'s module docstring for
why it was ruled out), and a Flask+Jinja2+HTMX review UI at curator.wittycomp.com.

Design: see `docs/superpowers/specs/2026-10-29-audio-ingress-pipeline-design.md`
in `wittycomp-lab`.
Plan: see `docs/superpowers/plans/2026-10-29-audio-ingress-pipeline.md` in
`wittycomp-lab`.

### Added, 2026-10-30/31 (same major version, UI/validation follow-on)

- **Acquire New** page (`/acquire`): manual usenet search by artist and/or
  album, grab directly without Sonarr/Prowlarr/SABnzbd. Results' artist/album
  fields are pre-filled by parsing each release's own scene-style title
  (`guess_artist_album()`), not just echoing the search box.
- **Watchlist** (`/watchlist`, `watchlist.py` + `watchlist_poll.py`): standing
  watch on an artist/album, polled against Prowlarr on a schedule
  (`WATCHLIST_INTERVAL_HOURS`, default 12h, plus an immediate check on add).
  Surfaces pending candidates for one-click approval -- deliberately never
  auto-grabs.
- **History** page (`/history`): last 200 resolved review_items, outcome
  badge + full reason text. Previously a resolved item left zero visible
  trace anywhere.
- **Post-promotion metadata check**: usenet-acquired files previously had
  their track count and decode integrity validated but never their tags or
  embedded art. `check_metadata()` moved to `lib/validator.py` (shared with
  the ingress-sweeper/health-scan paths) and now runs against every
  usenet-promoted file at its final library path; a gap leaves the row
  `pending` instead of `resolved` so it flows back through the normal
  relabel/art-select review flow.
- Review queue redesigned: color-coded status/category badges, live
  SABnzbd state on the detail page, inline per-row action feedback
  (previously rendered off-screen below long result lists).
- Fixed a real, previously-undiscovered production bug in `check_metadata()`:
  any FLAC with Vorbis comments set and no embedded picture (i.e. most real
  FLAC files) crashed the art-detection branch (`.tags` is a list of
  `(key, value)` tuples for FLAC, not a dict like ID3), silently misreported
  as a generic "metadata error" instead of the real "no album art" finding.
  Affected the weekly health scan the whole time this check has existed, not
  just the new usenet flow.

## [1.x] — Retroactive (prior generation)

The original `wn-curator-01` daemon: ffprobe-based corruption detection,
mutagen-based metadata/art-gap detection, ingress deduplication, stale-ingress
alerting, and Navidrome rescan triggering. No review UI, no usenet integration.
