# Changelog — Asaph

All notable changes to Asaph (the Wittycomp Lab music curator) are tracked here.

## [2.0.0] — Unreleased

Major version jump marking Asaph's expansion from a passive health-checker into
a full audio ingress pipeline: shared validator (decode-check + signal-heuristic
+ checksum-verify), extended ingress-sweeper with metadata/art resolution,
a scheduled library health scan, a Lidarr-based usenet acquisition + supersede
flow, and a Flask+Jinja2+HTMX review UI at curator.wittycomp.com.

Design: see `docs/superpowers/specs/2026-10-29-audio-ingress-pipeline-design.md`
in `wittycomp-lab`.
Plan: see `docs/superpowers/plans/2026-10-29-audio-ingress-pipeline.md` in
`wittycomp-lab`.

## [1.x] — Retroactive (prior generation)

The original `wn-curator-01` daemon: ffprobe-based corruption detection,
mutagen-based metadata/art-gap detection, ingress deduplication, stale-ingress
alerting, and Navidrome rescan triggering. No review UI, no usenet integration.
