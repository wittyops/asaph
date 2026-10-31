# Asaph

Wittycomp Lab's music library curator and audio ingress pipeline.

> "Kenaniah, leader of the Levites, was in charge of the singing; he instructed
> in singing because he was skillful." — 1 Chronicles 15:22
>
> Asaph himself: chief musician appointed by David, 1 Chronicles 16.

## What this is

The application code for `wn-curator-01` — the daemon that watches
`music/ingress/`, validates and tags incoming audio, scans the existing
library for corruption/metadata gaps, and (from v2.0.0) runs a review UI
and a direct Prowlarr+SABnzbd usenet acquisition/replacement flow for
files that fail validation. Lidarr is deliberately **not** part of this
flow — it enforces one artist record per MusicBrainz ID globally, so it
can't be given a disposable staging identity, and its only root folder is
the live library itself (zero gate on anything it imports). See
`usenet_flow.py`'s module docstring for the full rationale.

Deployment glue (compose.yaml, Dockerfile, .env) lives in the infra monorepo
at `wittycomp-lab/apps/wn-curator-01/`, which builds its image from a checkout
of this repo — same split used for `musicbrainz`.

## The web UI (curator.wittycomp.com, Authelia-gated)

Four pages, server-rendered Flask + Jinja2 + HTMX (no SPA framework, no
build step):

- **Review Queue** (`/review`) — anything the validator/resolver couldn't
  resolve on its own: corrupt files, missing tags/art, usenet grabs
  awaiting approval. Approve, relabel, pick art, or search usenet for a
  replacement, all inline.
- **Acquire New** (`/acquire`) — search usenet directly by artist and/or
  album (either field alone is enough) and grab a specific release without
  opening Sonarr, Prowlarr, or SABnzbd. Each result's artist/album is
  guessed from its own release title (`guess_artist_album()` in
  `usenet_flow.py`) so most rows need no typing before you grab.
- **Watchlist** (`/watchlist`) — stand up a watch on an artist (optionally
  one specific album); a background job checks Prowlarr on a schedule and
  lists new finds as pending candidates. Deliberately **not** a blind
  auto-grabber — every other resolution path in this codebase treats an
  unreviewed match as unsafe to act on, so a watchlist hit still needs one
  human click (`watchlist.py` / `watchlist_poll.py`).
- **History** (`/history`) — the last 200 resolved items, newest first,
  with a color-coded outcome badge and the full reason text: what was
  chosen, whether it was promoted into the library, and whether its
  metadata came through clean.

## Validation, not just acquisition

A usenet grab is checked twice before it's considered done, not once:
`check_completeness()` validates track count and decode integrity before
a file is ever promoted into the library (`usenet_flow.py`), then
`usenet_poll.py` runs `check_metadata()` (tags + embedded/folder art,
shared with the ingress-sweeper and health-scan paths via
`lib/validator.py`) against the file at its *final* library path. A
metadata gap doesn't block or reverse the promotion — the file is still
the best available copy either way — but the row stays `pending` instead
of `resolved`, flowing back through the same Review Queue UI (relabel /
art-select) for a human fix, rather than silently calling an
incompletely-tagged file "done."

## Version

See `VERSION` / `CHANGELOG.md`. Current: **2.0.0**.

## Design & plan

- Spec: `wittycomp-lab/docs/superpowers/specs/2026-10-29-audio-ingress-pipeline-design.md`
- Implementation plan: `wittycomp-lab/docs/superpowers/plans/2026-10-29-audio-ingress-pipeline.md`

## Remotes

- `origin` — Forgejo (primary): `http://10.10.30.250:3000/bearboss/asaph.git`
- `github` — GitHub mirror (read-only, for external visibility/backup):
  `https://github.com/wittyops/asaph.git`

Push `origin` first (or alongside); never treat `github` as authoritative.
