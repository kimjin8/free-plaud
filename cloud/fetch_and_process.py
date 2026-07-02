#!/usr/bin/env python3
"""
Cloud orchestrator: pull new audio from Plaud + a Google Drive intake folder,
run the validated transcribe+notes engine on each item, and write the results to
a Google Drive destination folder.

Designed to run as a nightly cron on a single Linux host (GCP free-tier e2-micro,
but host-agnostic — works on a Pi/VPS). See cloud-pipeline-DESIGN.md for the full
design and cloud/SETUP.md for the runbook.

This file does NOT reimplement transcription or notes generation. It reuses the
already-validated engine in plaud_pipeline.py verbatim (aai_upload, aai_transcribe,
transcript_to_text, gemini_notes, resolve_model, slugify_title).

Sources:
  * Plaud   — via the `plaud` CLI (@plaud-ai/cli). No --json flag exists (v0.3.2),
              so stdout is parsed; all CLI interaction is isolated below so a CLI
              format change is a one-spot fix.
  * Intake  — a Google Drive folder, via `rclone` (authenticated as the user).

Idempotency:
  * Plaud  — recording IDs are recorded in <state_dir>/processed_ids.txt; known IDs
             are skipped.
  * Intake — the original is moved into the intake folder's processed/ subfolder
             after success; that move IS the "done" marker.
  * A failed item is never marked processed, so it retries on the next run. One bad
    item never aborts the run (per-item try/except).

Logs go to stdout/stderr only (cron -> journald -> Cloud Logging). Nothing is ever
written to the destination Drive folder except transcripts and notes.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import requests

# Reuse the validated engine verbatim. The orchestrator lives in cloud/, the engine
# at the repo root, so make the repo root importable regardless of CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from plaud_pipeline import (  # noqa: E402
    aai_upload,
    aai_transcribe,
    transcript_to_text,
    gemini_notes,
    resolve_model,
    slugify_title,
)

# ----------------------------------------------------------------- configuration

# Constants from the design doc; every one is overridable via the environment so
# the same script runs on a Pi/VPS with different folders.
INTAKE_FOLDER_ID = os.environ.get("INTAKE_FOLDER_ID", "19cuAhqhrWY9BTI2_XZgNQlHWZYu4XTXF")
DEST_FOLDER_ID = os.environ.get("DEST_FOLDER_ID", "1AKFqlZ7RyRXa9Ge_y9221cZzRymjV9jo")
PLAUD_LOOKBACK_DAYS = int(os.environ.get("PLAUD_LOOKBACK_DAYS", "2"))
RCLONE_REMOTE = os.environ.get("RCLONE_REMOTE", "gdrive")
ARCHIVE_PLAUD_AUDIO = os.environ.get("ARCHIVE_PLAUD_AUDIO", "false").lower() == "true"
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")

# Persistent state lives on the host disk (NOT in Drive). Defaults to the same
# directory as the env file the design specifies.
STATE_DIR = Path(os.environ.get("STATE_DIR", str(Path.home() / ".plaud_pipeline"))).expanduser()
PROCESSED_IDS_FILE = STATE_DIR / "processed_ids.txt"

# Intake audio extensions (per handoff). Mirrors the engine's set closely.
AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".ogg", ".flac", ".aac", ".mp4", ".opus"}

# Optional host-agnostic alert hook (e.g. "msmtp you@example.com").
ALERT_CMD = os.environ.get("ALERT_CMD", "").strip()

# Failure notification: open a GitHub issue when a run fails. Both must be set
# (from Doppler) for notifications to fire; otherwise it's a no-op.
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.environ.get("GITHUB_REPO", "").strip()  # "owner/repo"

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# A plausible Plaud recording id: a single no-whitespace token of id-ish chars.
PLAUD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,}$")

# The plaud CLI's own exit codes (from its ExitCode enum, verified against v0.3.2).
PLAUD_EXIT_OK = 0
PLAUD_EXIT_AUTH_FAILED = 2
PLAUD_EXIT_UNREACHABLE = 3
PLAUD_EXIT_TIMEOUT = 4


class PlaudAuthError(Exception):
    """Raised when the Plaud CLI reports AUTH_FAILED (exit 2)."""


# ------------------------------------------------------------------- small utils

def log(msg: str) -> None:
    print(msg, flush=True)


def err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


def emit_auth_alert() -> None:
    """Emit the distinctive line a Cloud Logging log-based alert matches, and fire
    the optional host-agnostic ALERT_CMD if configured."""
    line = ("[ALERT][PLAUD_AUTH_FAILED] Plaud token expired or invalid. "
            "Re-run `plaud login` on the host and the job will resume next run.")
    err(line)
    if ALERT_CMD:
        try:
            subprocess.run(ALERT_CMD, shell=True, input=line, text=True, timeout=30, check=False)
        except Exception as e:  # alerting must never crash the run
            err(f"[warn] ALERT_CMD failed: {e}")


def notify_github_issue(title: str, body: str) -> None:
    """Open a GitHub issue to report a failed run. No-op unless GITHUB_TOKEN and
    GITHUB_REPO are set. Deduped: if an open issue with the same title already
    exists, skip it — so a persistent failure doesn't file a new issue every night.
    Never raises; notification must not crash the run."""
    if not (GITHUB_TOKEN and GITHUB_REPO):
        return
    api = f"https://api.github.com/repos/{GITHUB_REPO}/issues"
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}",
               "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    try:
        existing = requests.get(api, headers=headers, params={"state": "open"}, timeout=30)
        if existing.ok and any(i.get("title") == title for i in existing.json()):
            err(f"[notify] open issue already exists ({title!r}); not duplicating")
            return
        r = requests.post(api, headers=headers, json={"title": title, "body": body}, timeout=30)
        if r.ok:
            err(f"[notify] opened GitHub issue #{r.json().get('number')}")
        else:
            err(f"[notify] GitHub issue create failed ({r.status_code}): {r.text[:200]}")
    except Exception as e:
        err(f"[notify] GitHub notify error: {e}")


# ------------------------------------------------------------- plaud CLI adapter
# All Plaud CLI interaction is isolated here. The CLI is young and changes often;
# if its output format shifts, this is the only place to update.

def _run_plaud(args: list[str]) -> str:
    """Run `plaud <args>`, returning stdout. Raises PlaudAuthError on AUTH_FAILED;
    raises RuntimeError on other non-zero exits."""
    env = dict(os.environ, NO_COLOR="1", FORCE_COLOR="0")
    proc = subprocess.run(
        ["plaud", *args],
        capture_output=True, text=True, env=env, timeout=120,
    )
    if proc.returncode == PLAUD_EXIT_AUTH_FAILED:
        raise PlaudAuthError(strip_ansi(proc.stderr).strip() or "AUTH_FAILED")
    if proc.returncode != PLAUD_EXIT_OK:
        detail = strip_ansi(proc.stderr or proc.stdout).strip()
        raise RuntimeError(f"plaud {' '.join(args)} exited {proc.returncode}: {detail}")
    return proc.stdout


def plaud_version() -> str:
    try:
        return strip_ansi(_run_plaud(["version"])).strip().replace("\n", " ")
    except Exception:
        return "unknown"


def plaud_recent_ids(days: int) -> list[str]:
    """Return recording IDs from the last `days` days, newest first.

    Parses the `recent` table: each data row is `  <id>  <name>  <date>  <dur>`
    with the id as the first whitespace token. Header/summary/empty lines are
    skipped, so a format tweak degrades to "found nothing" rather than garbage."""
    out = _run_plaud(["recent", "--days", str(days)])
    ids: list[str] = []
    for raw in out.splitlines():
        clean = strip_ansi(raw)
        # Data rows are always indented (the CLI prints "  <id>  <name> ...").
        # Summary/header lines start at column 0, so this filters them out.
        if not clean[:1].isspace():
            continue
        line = clean.strip()
        if not line:
            continue
        token = line.split()[0]
        if token == "ID":  # column header on `files`
            continue
        if PLAUD_ID_RE.match(token):
            ids.append(token)
    # Dedupe, preserve order.
    seen, uniq = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            uniq.append(i)
    return uniq


def plaud_audio_url(file_id: str) -> str | None:
    """Return the 24-hour presigned audio URL for a recording, or None if the
    recording has no audio available."""
    out = _run_plaud(["audio", file_id])
    for raw in out.splitlines():
        line = strip_ansi(raw).strip()
        if line.startswith("http://") or line.startswith("https://"):
            return line
    return None  # "Audio not available for this recording."


def plaud_recording_time(file_id: str) -> str | None:
    """Return the recording's start time as a filesystem-safe 'YYYY-MM-DD HH_MM_SS'
    string (from `plaud file`), so output filenames carry the meeting date/time.
    Prefers start_at (actual recording start) over created_at. Returns None if
    unavailable — the caller falls back to the id."""
    try:
        out = _run_plaud(["file", file_id])
    except PlaudAuthError:
        raise
    except Exception:
        return None
    fields = {}
    for raw in out.splitlines():
        m = re.match(r"(start_at|created_at):\s*(.+)$", strip_ansi(raw).strip())
        if m:
            fields[m.group(1)] = m.group(2).strip()
    for key in ("start_at", "created_at"):
        val = fields.get(key)
        if val and val != "-":
            try:
                return datetime.fromisoformat(val).strftime("%Y-%m-%d %H_%M_%S")
            except ValueError:
                continue
    return None


# -------------------------------------------------------------- rclone adapter
# Google Drive I/O via rclone, authenticated as the user (OAuth token in rclone's
# config). Files are owned by the user, so writes to a personal-Gmail Drive work
# (a service account has no storage quota and cannot create files there).

def _run_rclone(args: list[str]) -> str:
    proc = subprocess.run(["rclone", *args], capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(f"rclone {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def rclone_intake_files() -> list[str]:
    """Top-level audio files in the intake folder (does not recurse, so the
    processed/ subfolder is naturally excluded)."""
    out = _run_rclone([
        "lsf", f"{RCLONE_REMOTE}:", "--files-only",
        "--drive-root-folder-id", INTAKE_FOLDER_ID,
    ])
    return [n.strip() for n in out.splitlines()
            if n.strip() and Path(n.strip()).suffix.lower() in AUDIO_EXTS]


def rclone_copy_from_intake(name: str, dest_dir: Path) -> Path:
    _run_rclone([
        "copy", f"{RCLONE_REMOTE}:{name}", str(dest_dir),
        "--drive-root-folder-id", INTAKE_FOLDER_ID,
    ])
    return dest_dir / name


def rclone_copy_to_dest(local_file: Path) -> None:
    _run_rclone([
        "copy", str(local_file), f"{RCLONE_REMOTE}:",
        "--drive-root-folder-id", DEST_FOLDER_ID,
    ])


def rclone_move_intake_to_processed(name: str) -> None:
    _run_rclone([
        "moveto", f"{RCLONE_REMOTE}:{name}", f"{RCLONE_REMOTE}:processed/{name}",
        "--drive-root-folder-id", INTAKE_FOLDER_ID,
    ])


# --------------------------------------------------------------- state handling

def load_processed_ids() -> set[str]:
    if not PROCESSED_IDS_FILE.exists():
        return set()
    return {ln.strip() for ln in PROCESSED_IDS_FILE.read_text(encoding="utf-8").splitlines()
            if ln.strip()}


def mark_processed_id(file_id: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(PROCESSED_IDS_FILE, "a", encoding="utf-8") as f:
        f.write(file_id + "\n")


# --------------------------------------------------------------- core processing

def _sanitize_base(name: str) -> str:
    keep = "".join(c if c.isalnum() or c in " -_()&" else "_" for c in name)
    return keep.strip()[:80] or "recording"


def transcribe_and_note(audio_path: Path, base: str, work_dir: Path,
                        aai_key: str, gem_key: str, model: str) -> list[Path]:
    """Run the validated engine on one local audio file and write the transcript +
    notes into work_dir. Returns the list of output files to upload to Drive."""
    transcript_path = work_dir / f"{base}.transcript.txt"
    notes_path = work_dir / f"{base}.notes.md"

    log(f"      uploading to AssemblyAI ...")
    audio_url = aai_upload(audio_path, aai_key)
    log(f"      transcribing ...")
    data = aai_transcribe(audio_url, aai_key)
    transcript = transcript_to_text(data)
    transcript_path.write_text(transcript, encoding="utf-8")
    log(f"      transcript: {transcript_path.name} ({len(transcript)} chars)")

    log(f"      generating notes ({model}) ...")
    notes = gemini_notes(transcript, gem_key, model)
    notes_path.write_text(notes, encoding="utf-8")
    title = slugify_title(notes)
    if title:
        nice = work_dir / f"{base} - {title}.notes.md"
        if nice != notes_path and not nice.exists():
            notes_path.rename(nice)
            notes_path = nice
    log(f"      notes: {notes_path.name}")
    return [transcript_path, notes_path]


def process_plaud(processed: set[str], aai_key: str, gem_key: str, model: str,
                  dry_run: bool) -> tuple[int, int]:
    """Process new Plaud recordings. Returns (ok_count, error_count).
    Raises PlaudAuthError if the CLI can't authenticate."""
    ids = plaud_recent_ids(PLAUD_LOOKBACK_DAYS)
    new_ids = [i for i in ids if i not in processed]
    log(f"[plaud] {len(ids)} in last {PLAUD_LOOKBACK_DAYS}d, {len(new_ids)} new")

    ok = errors = 0
    for file_id in new_ids:
        if dry_run:
            log(f"[plaud] WOULD process: {file_id}")
            continue
        log(f"[plaud] processing {file_id}")
        try:
            url = plaud_audio_url(file_id)
            if not url:
                log(f"      [skip] no audio available yet for {file_id} (will retry)")
                continue  # not marked processed -> retried next run
            with tempfile.TemporaryDirectory(prefix="plaud_") as td:
                work = Path(td)
                audio_path = work / f"{file_id}.audio"
                _download(url, audio_path)
                # Name outputs by the recording's date/time so notes are
                # distinguishable; fall back to the id if unavailable.
                base = plaud_recording_time(file_id) or f"plaud_{_sanitize_base(file_id)}"
                outputs = transcribe_and_note(audio_path, base, work,
                                              aai_key, gem_key, model)
                for f in outputs:
                    rclone_copy_to_dest(f)
                if ARCHIVE_PLAUD_AUDIO:
                    rclone_copy_to_dest(audio_path)
            mark_processed_id(file_id)  # only after full success
            ok += 1
            log(f"[plaud] done {file_id}")
        except PlaudAuthError:
            raise
        except Exception as e:
            errors += 1
            err(f"[plaud] ERROR {file_id}: {e}")  # not marked -> retried next run
    return ok, errors


def process_intake(aai_key: str, gem_key: str, model: str,
                   dry_run: bool) -> tuple[int, int]:
    """Process audio files in the Drive intake folder. Returns (ok, errors)."""
    files = rclone_intake_files()
    log(f"[intake] {len(files)} audio file(s) to process")

    ok = errors = 0
    for name in files:
        if dry_run:
            log(f"[intake] WOULD process: {name}")
            continue
        log(f"[intake] processing {name}")
        try:
            with tempfile.TemporaryDirectory(prefix="intake_") as td:
                work = Path(td)
                local = rclone_copy_from_intake(name, work)
                base = _sanitize_base(Path(name).stem)
                outputs = transcribe_and_note(local, base, work,
                                              aai_key, gem_key, model)
                for o in outputs:
                    rclone_copy_to_dest(o)
            rclone_move_intake_to_processed(name)  # the "done" marker
            ok += 1
            log(f"[intake] done {name}")
        except Exception as e:
            errors += 1
            err(f"[intake] ERROR {name}: {e}")  # not moved -> retried next run
    return ok, errors


def _download(url: str, dest: Path) -> None:
    with requests.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                if chunk:
                    f.write(chunk)


# ------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="Plaud + Drive intake -> transcribe/notes -> Drive.")
    ap.add_argument("--dry-run", action="store_true",
                    help="List what WOULD be processed; no downloads, no paid API calls.")
    ap.add_argument("--source", choices=["all", "plaud", "intake"], default="all",
                    help="Limit to one source (default: all).")
    args = ap.parse_args()

    # Only require the tools the selected sources actually use.
    needed = []
    if args.source in ("all", "intake"):
        needed.append("rclone")
    if args.source in ("all", "plaud"):
        needed.append("plaud")
    for tool in needed:
        if shutil.which(tool) is None:
            err(f"ERROR: required tool not found on PATH: {tool}")
            return 1

    aai_key = os.environ.get("ASSEMBLYAI_API_KEY")
    gem_key = os.environ.get("GEMINI_API_KEY")
    model = GEMINI_MODEL
    if not args.dry_run:
        if not aai_key or not gem_key:
            err("ERROR: set ASSEMBLYAI_API_KEY and GEMINI_API_KEY.")
            return 1
        model = resolve_model(GEMINI_MODEL, gem_key)

    log(f"plaud CLI: {plaud_version()}")
    log(f"model: {model} | lookback: {PLAUD_LOOKBACK_DAYS}d | "
        f"archive_plaud_audio: {ARCHIVE_PLAUD_AUDIO} | dry_run: {args.dry_run}")

    intake_ok = intake_err = plaud_ok = plaud_err = 0
    auth_failed = False

    # Intake first so a Plaud auth failure never discards intake results.
    if args.source in ("all", "intake"):
        try:
            intake_ok, intake_err = process_intake(aai_key, gem_key, model, args.dry_run)
        except Exception as e:
            intake_err += 1
            err(f"[intake] FATAL: {e}")

    if args.source in ("all", "plaud"):
        processed = load_processed_ids()
        try:
            plaud_ok, plaud_err = process_plaud(processed, aai_key, gem_key, model, args.dry_run)
        except PlaudAuthError:
            auth_failed = True
            emit_auth_alert()
        except Exception as e:
            plaud_err += 1
            err(f"[plaud] FATAL: {e}")

    total_ok = intake_ok + plaud_ok
    total_err = intake_err + plaud_err
    summary = f"processed={total_ok} intake={intake_ok} plaud={plaud_ok} errors={total_err}"
    log(summary)

    # Notify on failure (GitHub issue). Dry-runs never notify.
    if not args.dry_run and (auth_failed or total_err):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        if auth_failed:
            notify_github_issue(
                "[plaud-pipeline] Plaud login expired — re-auth needed",
                "The nightly run could not authenticate to Plaud (AUTH_FAILED).\n\n"
                "**Fix:** re-run `plaud login` (or re-seed `tokens.json`) on teslamate-vm.\n\n"
                f"- {summary}\n- {ts}")
        else:
            notify_github_issue(
                "[plaud-pipeline] Nightly run reported errors",
                f"The nightly run finished with {total_err} error(s).\n\n"
                "**Check:** `journalctl -u plaud-pipeline.service --since today` on teslamate-vm.\n\n"
                f"- {summary}\n- {ts}")

    if auth_failed:
        return PLAUD_EXIT_AUTH_FAILED  # exit non-zero so the alert policy fires
    return 1 if total_err else 0


if __name__ == "__main__":
    sys.exit(main())
