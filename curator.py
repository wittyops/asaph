#!/usr/bin/env python3
"""
wn-curator-01 — Music Library Curator
Monitors ingress + library for corrupt, incomplete, and metadata-deficient music.
Triggers Navidrome scans, quarantines corrupt ingress files, and sends ntfy alerts.
"""
import os
import re
import json
import sqlite3
import hashlib
import threading
import subprocess
import time
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, redirect
from apscheduler.schedulers.background import BackgroundScheduler
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import requests as http_requests
import mutagen

# ── Config ────────────────────────────────────────────────────────────────────

LIBRARY_PATHS = [p for p in [
    Path("/library"),
    Path("/christian-library"),
    Path("/instrumentals-library"),
] if p.exists()]

INGRESS_PATH = Path("/ingress")
STATE_DB = Path("/state/curator.db")
QUARANTINE_PATH = INGRESS_PATH / "quarantine"
REVIEW_DB = Path("/state/review.db")

NAVIDROME_HOST = os.environ.get("NAVIDROME_HOST", "http://10.10.30.250:4533")
NAVIDROME_USER = os.environ.get("NAVIDROME_USER", "")
NAVIDROME_PASS = os.environ.get("NAVIDROME_PASS", "")
NTFY_URL = os.environ.get("NTFY_URL", "")
NTFY_USER = os.environ.get("NTFY_USER", "")
NTFY_PASS = os.environ.get("NTFY_PASS", "")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "curator")
STALE_INGRESS_HOURS = int(os.environ.get("STALE_INGRESS_HOURS", "4"))
SCAN_INTERVAL_HOURS = int(os.environ.get("SCAN_INTERVAL_HOURS", "6"))
API_PORT = int(os.environ.get("API_PORT", "9000"))

AUDIO_EXTENSIONS = {'.flac', '.mp3', '.m4a', '.ogg', '.opus', '.wav', '.aac'}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S"
)
log = logging.getLogger("curator")

state = {
    "status": "initializing",
    "last_scan": None,
    "scan_in_progress": False,
    "last_report": {},
    "errors": [],
    "library_index": set(),
}
state_lock = threading.Lock()

# ── Database ──────────────────────────────────────────────────────────────────

def init_db():
    STATE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(STATE_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS checked_files (
            path TEXT PRIMARY KEY,
            mtime REAL,
            checked_at TEXT,
            issues TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS quarantined (
            path TEXT PRIMARY KEY,
            original_path TEXT,
            quarantined_at TEXT,
            reason TEXT
        )
    """)
    conn.commit()
    conn.close()

def db_connect():
    return sqlite3.connect(str(STATE_DB), timeout=10)

# ── Navidrome Subsonic API ────────────────────────────────────────────────────

def _subsonic_token():
    salt = "wittycomp"
    token = hashlib.md5((NAVIDROME_PASS + salt).encode()).hexdigest()
    return token, salt

def subsonic_request(endpoint, params=None):
    if not NAVIDROME_USER or not NAVIDROME_PASS:
        return None
    token, salt = _subsonic_token()
    base_params = {
        "u": NAVIDROME_USER, "t": token, "s": salt,
        "v": "1.16.1", "c": "wn-curator-01", "f": "json",
    }
    if params:
        base_params.update(params)
    try:
        r = http_requests.get(
            f"{NAVIDROME_HOST}/rest/{endpoint}",
            params=base_params, timeout=10
        )
        r.raise_for_status()
        return r.json().get("subsonic-response", {})
    except Exception as e:
        log.error(f"Subsonic {endpoint}: {e}")
        return None

def navidrome_trigger_scan():
    resp = subsonic_request("startScan")
    if resp and resp.get("status") == "ok":
        log.info("Navidrome scan triggered")
        return True
    log.warning("Navidrome scan trigger failed (API unreachable or creds missing)")
    return False

# ── ntfy ─────────────────────────────────────────────────────────────────────

def ntfy_send(title, body, priority="default", tags=None):
    if not NTFY_URL:
        log.info(f"[ntfy skipped] {title}: {body}")
        return
    headers = {"Title": title, "Priority": priority}
    if tags:
        headers["Tags"] = ",".join(tags)
    auth = (NTFY_USER, NTFY_PASS) if NTFY_USER and NTFY_PASS else None
    try:
        http_requests.post(
            f"{NTFY_URL}/{NTFY_TOPIC}",
            data=body.encode(), headers=headers, auth=auth, timeout=5
        )
    except Exception as e:
        log.error(f"ntfy send failed: {e}")

# ── Audio Integrity ───────────────────────────────────────────────────────────

def check_integrity(path: Path):
    """Returns error string if corrupt/incomplete, None if OK."""
    try:
        if path.stat().st_size < 1024:
            return "file too small (<1KB), likely incomplete"
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0 or "audio" not in result.stdout:
            err = result.stderr.strip()[:200]
            return f"ffprobe: {err or 'no audio stream'}"
        return None
    except subprocess.TimeoutExpired:
        return "ffprobe timeout (>30s)"
    except Exception as e:
        return f"integrity check error: {e}"

# ── Metadata Inspection ───────────────────────────────────────────────────────

def check_metadata(path: Path):
    """Returns list of metadata issue strings."""
    issues = []
    try:
        audio = mutagen.File(str(path), easy=True)
        if audio is None:
            return ["mutagen: unrecognized format"]
        for tag in ["title", "artist", "album"]:
            val = audio.get(tag, [])
            if not val or not str(val[0]).strip():
                issues.append(f"missing {tag}")

        # Album art: check embedded, then folder
        audio_raw = mutagen.File(str(path))
        has_art = False
        if audio_raw:
            if hasattr(audio_raw, "pictures") and audio_raw.pictures:
                has_art = True
            elif hasattr(audio_raw, "tags") and audio_raw.tags:
                has_art = any(k.startswith("APIC") for k in audio_raw.tags)
            elif "covr" in (audio_raw or {}):
                has_art = True

        if not has_art:
            album_dir = path.parent
            folder_art_names = ["albumart", "cover", "folder", "front", "AlbumArt"]
            has_folder_art = any(
                list(album_dir.glob(f"{name}.*"))
                for name in folder_art_names
            )
            if not has_folder_art:
                issues.append("no album art (embedded or folder)")
    except Exception as e:
        issues.append(f"metadata error: {e}")
    return issues

# ── Stale Ingress ─────────────────────────────────────────────────────────────

def check_stale_ingress():
    stale = []
    if not INGRESS_PATH.exists():
        return stale
    cutoff = time.time() - (STALE_INGRESS_HOURS * 3600)
    for p in INGRESS_PATH.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        if "quarantine" in p.parts:
            continue
        try:
            mtime = p.stat().st_mtime
            if mtime < cutoff:
                stale.append({
                    "path": str(p),
                    "age_hours": round((time.time() - mtime) / 3600, 1),
                })
        except Exception:
            pass
    return stale

# ── Deduplication ────────────────────────────────────────────────────────────

_YT_ID_RE = re.compile(r'\s*\[[A-Za-z0-9_-]{11}\]')
_YT_ID_11 = re.compile(r'^[A-Za-z0-9_-]{11}$')

def _yt_id_from_sidecar(path: Path):
    """Return the 11-char YouTube video ID from an adjacent .info.json sidecar.

    yt-dlp writes <stem>.info.json alongside the downloaded audio file.
    The ID is the ground-truth identity of the source video — stronger than
    metadata or PURL for dedup purposes.
    """
    sidecar = path.with_suffix('.info.json')
    if sidecar.exists():
        try:
            data = json.loads(sidecar.read_text())
            yt_id = str(data.get('id', '')).strip()
            if _YT_ID_11.match(yt_id):
                return yt_id
        except Exception:
            pass
    return None

def _metadata_key(path: Path):
    """Return 'artist|album|title[|purl]' key for a file, or None if untagged.

    PURL (source URL embedded by yt-dlp in OPUS Vorbis comments) is appended
    as a tiebreaker so remix/original pairs with identical tags stay distinct.
    """
    try:
        audio = mutagen.File(str(path), easy=True)
        if audio is None:
            return None
        title  = str(audio.get('title',  [''])[0]).strip().lower()
        artist = str(audio.get('artist', [''])[0]).strip().lower()
        album  = str(audio.get('album',  [''])[0]).strip().lower()
        if title:
            key = f"{artist}|{album}|{title}"
            # Append PURL when present so same-title/different-source files
            # (e.g. original vs remix with identical embedded tags) stay distinct.
            try:
                raw = mutagen.File(str(path))
                if raw and raw.tags:
                    purl_vals = raw.tags.get('PURL') or raw.tags.get('purl') or []
                    purl = str(purl_vals[0]).strip() if purl_vals else ''
                    if purl:
                        key += f"|{purl}"
            except Exception:
                pass
            return key
    except Exception:
        pass
    return None

def _normalize_stem(stem: str) -> str:
    return _YT_ID_RE.sub('', stem).strip().lower()

def find_ingress_duplicates():
    """Return list of duplicate groups (>1 file share same metadata or normalized name).

    When a .info.json sidecar is present, the YouTube video ID is used as the
    primary dedup key — it's the ground truth for source identity and can
    correctly identify re-downloads of the same video even if tags differ.
    Falls back to metadata key (with PURL tiebreaker) then filename stem.
    """
    if not INGRESS_PATH.exists():
        return []
    groups = {}
    for p in INGRESS_PATH.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        if "quarantine" in p.parts or ".temp." in p.name:
            continue
        yt_id = _yt_id_from_sidecar(p)
        if yt_id:
            key = f"ytid:{yt_id}"
        else:
            key = _metadata_key(p) or f"name:{_normalize_stem(p.stem)}"
        groups.setdefault(key, []).append(str(p))
    return [
        {"key": k, "count": len(v), "paths": sorted(v)}
        for k, v in groups.items() if len(v) > 1
    ]

def ingress_sidecar_coverage():
    """Return counts of ingress audio files with and without .info.json sidecars."""
    if not INGRESS_PATH.exists():
        return {"ingress_audio_count": 0, "with_sidecar": 0, "without_sidecar": 0}
    audio_files = [
        p for p in INGRESS_PATH.rglob("*")
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
        and "quarantine" not in p.parts and ".temp." not in p.name
    ]
    with_sidecar = sum(1 for p in audio_files if p.with_suffix('.info.json').exists())
    return {
        "ingress_audio_count": len(audio_files),
        "with_sidecar": with_sidecar,
        "without_sidecar": len(audio_files) - with_sidecar,
    }

def find_library_clashes(library_index: set):
    """Return ingress files whose metadata key already exists in the library."""
    if not INGRESS_PATH.exists():
        return []
    clashes = []
    for p in INGRESS_PATH.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        if "quarantine" in p.parts or ".temp." in p.name:
            continue
        key = _metadata_key(p)
        if key and key in library_index:
            clashes.append({"path": str(p), "key": key})
    return clashes

# ── Quarantine ────────────────────────────────────────────────────────────────

def quarantine_file(path: Path, reason: str):
    QUARANTINE_PATH.mkdir(parents=True, exist_ok=True)
    dest = QUARANTINE_PATH / path.name
    if dest.exists():
        dest = QUARANTINE_PATH / f"{path.stem}_{int(time.time())}{path.suffix}"
    try:
        path.rename(dest)
        conn = db_connect()
        conn.execute(
            "INSERT OR REPLACE INTO quarantined (path,original_path,quarantined_at,reason) VALUES (?,?,?,?)",
            (str(dest), str(path), datetime.now(timezone.utc).isoformat(), reason)
        )
        conn.commit()
        conn.close()
        log.info(f"Quarantined {path.name}: {reason}")
        return str(dest)
    except Exception as e:
        log.error(f"Quarantine failed for {path}: {e}")
        return None

# ── Full Scan ─────────────────────────────────────────────────────────────────

def run_full_scan():
    with state_lock:
        if state["scan_in_progress"]:
            log.info("Scan already running — skipping")
            return
        state["scan_in_progress"] = True
        state["status"] = "scanning"

    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "library_file_count": 0,
        "files_checked": 0,
        "corrupt": [],
        "metadata_issues": [],
        "stale_ingress": [],
        "quarantined_this_run": [],
        "navidrome_scan_triggered": False,
        "ingress_duplicates": [],
        "library_clashes": [],
        "info_json_coverage": {},
    }

    try:
        log.info("=== Full scan starting ===")

        # 0. Deduplication + sidecar coverage
        report["info_json_coverage"] = ingress_sidecar_coverage()
        ingress_dupes = find_ingress_duplicates()
        report["ingress_duplicates"] = ingress_dupes
        if ingress_dupes:
            total_extra = sum(d["count"] - 1 for d in ingress_dupes)
            log.warning(f"Ingress duplicates: {len(ingress_dupes)} groups, {total_extra} redundant files")

        with state_lock:
            cached_idx = state["library_index"].copy()
        library_clashes = find_library_clashes(cached_idx)
        report["library_clashes"] = library_clashes
        if library_clashes:
            log.warning(f"Library clashes: {len(library_clashes)} ingress files already in library")

        # 1. Stale ingress
        report["stale_ingress"] = check_stale_ingress()
        if report["stale_ingress"]:
            log.warning(f"Stale ingress: {len(report['stale_ingress'])} files")

        # 2. Ingress corruption check (quarantine bad files)
        if INGRESS_PATH.exists():
            for p in INGRESS_PATH.rglob("*"):
                if not p.is_file() or p.suffix.lower() not in AUDIO_EXTENSIONS:
                    continue
                if "quarantine" in p.parts:
                    continue
                try:
                    age = time.time() - p.stat().st_mtime
                    if age < 1800:  # Skip files being written (<30min old)
                        continue
                except Exception:
                    continue
                err = check_integrity(p)
                if err:
                    dest = quarantine_file(p, err)
                    if dest:
                        report["quarantined_this_run"].append({
                            "original": str(p), "dest": dest, "reason": err
                        })

        # 3. Library integrity + metadata (incremental — skip unchanged files)
        # Also builds library_index for dedup clash detection
        conn = db_connect()
        file_count = 0
        checked = 0
        new_library_index = set()
        for lib_path in LIBRARY_PATHS:
            if not lib_path.exists():
                continue
            for audio_file in lib_path.rglob("*"):
                if not audio_file.is_file() or audio_file.suffix.lower() not in AUDIO_EXTENSIONS:
                    continue
                file_count += 1
                key = _metadata_key(audio_file)
                if key:
                    new_library_index.add(key)
                try:
                    mtime = audio_file.stat().st_mtime
                except Exception:
                    continue
                # Skip if unchanged since last check
                row = conn.execute(
                    "SELECT mtime FROM checked_files WHERE path=?", (str(audio_file),)
                ).fetchone()
                if row and row[0] == mtime:
                    continue
                checked += 1

                err = check_integrity(audio_file)
                if err:
                    log.warning(f"CORRUPT (library): {audio_file.name} — {err}")
                    report["corrupt"].append({"path": str(audio_file), "error": err})
                    # Don't quarantine library files — alert only
                else:
                    meta_issues = check_metadata(audio_file)
                    if meta_issues:
                        report["metadata_issues"].append({
                            "path": str(audio_file), "issues": meta_issues
                        })

                conn.execute(
                    "INSERT OR REPLACE INTO checked_files (path,mtime,checked_at,issues) VALUES (?,?,?,?)",
                    (str(audio_file), mtime, datetime.now(timezone.utc).isoformat(),
                     json.dumps([err] if err else check_metadata(audio_file) if not err else []))
                )

        conn.commit()
        conn.close()
        report["library_file_count"] = file_count
        report["files_checked"] = checked
        with state_lock:
            state["library_index"] = new_library_index

        # 4. Navidrome rescan
        if navidrome_trigger_scan():
            report["navidrome_scan_triggered"] = True

        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        cov = report["info_json_coverage"]
        log.info(
            f"=== Scan done: {file_count} library files ({checked} re-checked), "
            f"{len(report['corrupt'])} corrupt, {len(report['metadata_issues'])} metadata gaps, "
            f"{len(report['stale_ingress'])} stale ingress, "
            f"{len(report['quarantined_this_run'])} quarantined, "
            f"{len(report['ingress_duplicates'])} dup groups, "
            f"{len(report['library_clashes'])} lib clashes | "
            f"ingress sidecar: {cov.get('with_sidecar',0)}/{cov.get('ingress_audio_count',0)} ==="
        )

        # 5. ntfy summary (only if something noteworthy)
        issues = (len(report["corrupt"]) + len(report["quarantined_this_run"])
                  + len(report["stale_ingress"]) + len(report["ingress_duplicates"])
                  + len(report["library_clashes"]))
        if issues > 0:
            lines = [f"Library scan: {file_count} files ({checked} re-checked)"]
            if report["corrupt"]:
                lines.append(f"⚠ {len(report['corrupt'])} corrupt files in library (alert only)")
            if report["quarantined_this_run"]:
                lines.append(f"🔒 {len(report['quarantined_this_run'])} corrupt files quarantined from ingress")
            if report["stale_ingress"]:
                lines.append(f"⏰ {len(report['stale_ingress'])} stale files in ingress (>{STALE_INGRESS_HOURS}h)")
            if report["ingress_duplicates"]:
                total_extra = sum(d["count"] - 1 for d in report["ingress_duplicates"])
                lines.append(f"🔁 {len(report['ingress_duplicates'])} duplicate groups ({total_extra} redundant files) in ingress")
            if report["library_clashes"]:
                lines.append(f"📚 {len(report['library_clashes'])} ingress files already exist in library")
            ntfy_send(
                "Music Curator — Issues Found",
                "\n".join(lines),
                priority="high" if issues >= 5 else "default",
                tags=["musical_note", "warning"],
            )

        with state_lock:
            state["last_scan"] = report["finished_at"]
            state["last_report"] = report
            state["status"] = "idle"
            state["scan_in_progress"] = False

    except Exception as e:
        log.error(f"Scan error: {e}", exc_info=True)
        with state_lock:
            state["status"] = "error"
            state["scan_in_progress"] = False
            state["errors"].append({"time": datetime.now(timezone.utc).isoformat(), "error": str(e)})
        ntfy_send("Music Curator — Scan Error", str(e), priority="high", tags=["warning"])

# ── Watchdog ──────────────────────────────────────────────────────────────────

class IngressHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory and Path(event.src_path).suffix.lower() in AUDIO_EXTENSIONS:
            if "quarantine" not in event.src_path:
                log.info(f"New ingress file detected: {Path(event.src_path).name}")

# ── Flask API ─────────────────────────────────────────────────────────────────

app = Flask(__name__)

from review_queue import review_bp, REVIEW_DB_KEY
app.config[REVIEW_DB_KEY] = REVIEW_DB
app.config["PROWLARR_BASE"] = os.environ.get("PROWLARR_BASE", "http://10.10.30.53:9696")
app.config["PROWLARR_API_KEY"] = os.environ.get("PROWLARR_API_KEY", "")  # nosemgrep
app.config["SABNZBD_BASE"] = os.environ.get("SABNZBD_BASE", "http://wn-sabnzbd-01:8080")
app.config["SABNZBD_API_KEY"] = os.environ.get("SABNZBD_API_KEY", "")  # nosemgrep
app.config["SABNZBD_CATEGORY"] = os.environ.get("SABNZBD_CATEGORY", "asaph-staging")
app.register_blueprint(review_bp)

@app.route("/")
def api_index():
    return redirect("/review")

@app.route("/health")
def api_health():
    with state_lock:
        ok = state["status"] != "error"
    return jsonify({"status": "ok" if ok else "error"}), 200 if ok else 503

@app.route("/status")
def api_status():
    with state_lock:
        return jsonify({
            "status": state["status"],
            "last_scan": state["last_scan"],
            "scan_in_progress": state["scan_in_progress"],
            "library_paths": [str(p) for p in LIBRARY_PATHS],
            "ingress_path": str(INGRESS_PATH),
            "scan_interval_hours": SCAN_INTERVAL_HOURS,
            "recent_errors": state["errors"][-5:],
        })

@app.route("/report")
def api_report():
    with state_lock:
        return jsonify(state["last_report"])

@app.route("/scan", methods=["POST"])
def api_trigger_scan():
    t = threading.Thread(target=run_full_scan, daemon=True)
    t.start()
    return jsonify({"triggered": True, "message": "Full scan started in background"})

@app.route("/duplicates")
def api_duplicates():
    ingress_dupes = find_ingress_duplicates()
    with state_lock:
        lib_idx = state["library_index"].copy()
    lib_clashes = find_library_clashes(lib_idx)
    return jsonify({
        "ingress_duplicates": ingress_dupes,
        "library_clashes": lib_clashes,
        "duplicate_groups": len(ingress_dupes),
        "library_clash_count": len(lib_clashes),
        "library_index_size": len(lib_idx),
        "note": "library_index built during last full scan; run POST /scan to refresh",
    })

@app.route("/quarantine")
def api_quarantine():
    conn = db_connect()
    rows = conn.execute(
        "SELECT path,original_path,quarantined_at,reason FROM quarantined "
        "ORDER BY quarantined_at DESC LIMIT 100"
    ).fetchall()
    conn.close()
    return jsonify([
        {"path": r[0], "original": r[1], "quarantined_at": r[2], "reason": r[3]}
        for r in rows
    ])

# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    QUARANTINE_PATH.mkdir(parents=True, exist_ok=True)

    observer = Observer()
    if INGRESS_PATH.exists():
        observer.schedule(IngressHandler(), str(INGRESS_PATH), recursive=True)
    observer.start()

    scheduler = BackgroundScheduler()
    scheduler.add_job(run_full_scan, "interval", hours=SCAN_INTERVAL_HOURS, id="full_scan")
    scheduler.add_job(
        run_full_scan, "date",
        run_date=datetime.now() + timedelta(minutes=2),
        id="initial_scan"
    )

    from health_scan import init_review_db, scan_library
    init_review_db(REVIEW_DB)
    scheduler.add_job(
        lambda: scan_library(LIBRARY_PATHS, REVIEW_DB),
        "cron", day_of_week="sun", hour=3, id="library_health_scan",
    )

    from usenet_poll import poll_searching_items
    STAGING_ROOT = Path(os.environ.get("STAGING_ROOT", "/staging"))
    SUPERSEDE_ARCHIVE_ROOT = Path(os.environ.get("SUPERSEDE_ARCHIVE_ROOT", "/library/superseded"))
    LIBRARY_ROOT_FOR_SUPERSEDE = Path("/library")
    SABNZBD_STAGING_PREFIX = Path(os.environ.get("SABNZBD_STAGING_PREFIX", "/data/usenet/complete/asaph-staging"))
    scheduler.add_job(
        lambda: poll_searching_items(
            REVIEW_DB, app.config["SABNZBD_BASE"], app.config["SABNZBD_API_KEY"],
            STAGING_ROOT, LIBRARY_ROOT_FOR_SUPERSEDE, SUPERSEDE_ARCHIVE_ROOT,
            SABNZBD_STAGING_PREFIX,
        ),
        "interval", minutes=5, id="usenet_poll",
    )

    scheduler.start()

    with state_lock:
        state["status"] = "idle"

    log.info(f"wn-curator-01 started | API :{API_PORT} | scan every {SCAN_INTERVAL_HOURS}h | "
             f"stale threshold {STALE_INGRESS_HOURS}h | libraries: {[str(p) for p in LIBRARY_PATHS]}")
    ntfy_send("Music Curator", "wn-curator-01 started — initial scan in 2 min", tags=["musical_note"])

    app.run(host="0.0.0.0", port=API_PORT, threaded=True)
