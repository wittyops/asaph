# Asaph

Wittycomp Lab's music library curator and audio ingress pipeline.

> "Kenaniah, leader of the Levites, was in charge of the singing; he instructed
> in singing because he was skillful." — 1 Chronicles 15:22
>
> Asaph himself: chief musician appointed by David, 1 Chronicles 16.

## What this is

The application code for `wn-curator-01` — the daemon that watches
`music/ingress/`, validates and tags incoming audio, scans the existing
library for corruption/metadata gaps, and (from v2.0.0) runs a review UI and
a Lidarr-based usenet replacement flow for files that fail validation.

Deployment glue (compose.yaml, Dockerfile, .env) lives in the infra monorepo
at `wittycomp-lab/apps/wn-curator-01/`, which builds its image from a checkout
of this repo — same split used for `musicbrainz`.

## Version

See `VERSION` / `CHANGELOG.md`. Current: **2.0.0** (in development).

## Design & plan

- Spec: `wittycomp-lab/docs/superpowers/specs/2026-10-29-audio-ingress-pipeline-design.md`
- Implementation plan: `wittycomp-lab/docs/superpowers/plans/2026-10-29-audio-ingress-pipeline.md`
