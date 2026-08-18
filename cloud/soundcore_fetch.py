#!/usr/bin/env python3
"""
Soundcore fetcher: pull new recordings from the Anker Soundcore Online Hub and drop
the audio into the SAME Google Drive intake folder the nightly pipeline already
watches. Nothing downstream changes — fetch_and_process.py picks the files up via
process_intake() with zero code changes, because .ogg is already in AUDIO_EXTS.

Why a browser instead of an HTTP client:
  The Hub's API (anka-api-us.soundcore.com) is an encrypted, signed, anti-replay
  protocol (ECDH P-256 + AES-GCM + HMAC-SHA256, keyed per session). Even the HLS
  playlist on S3 is ciphertext at rest. Replaying its calls from Python means
  reimplementing that handshake out of a minified bundle Anker can rotate at will.
  So we drive the real app and let its own JS do the crypto, then harvest the file
  the Hub's own "Export Audio" action produces.

Where this runs:
  On the same 1 GB host as the nightly job, as one program. Measured: headless
  Chromium peaks around 250 MB there with a real session, so the container is capped
  (see cloud/host-run.sh). If the browser ever overruns, Docker kills this container
  and the co-tenant services are untouched.

Login is manual and one-time (`--login`), exactly like the rclone OAuth ritual in
cloud/DEPLOY.md. This script never sees or stores an Anker password.

Modes:
  --login     Open a visible browser, wait for you to sign in, persist the profile.
  --probe     Dump the Hub's list structure so the selectors below can be pinned.
  --dry-run   Enumerate and report what WOULD be exported. No downloads, no uploads.
  (default)   Export each new recording and upload it to the Drive intake folder.
"""

import argparse
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

# Reuse the nightly job's Drive adapter and failure plumbing rather than growing a
# second copy. This file lives beside it in cloud/.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_and_process import (  # noqa: E402
    INTAKE_FOLDER_ID,
    RCLONE_REMOTE,
    _run_rclone,
    err,
    log,
    notify_github_issue,
)

# ----------------------------------------------------------------- configuration

HUB_URL = "https://ai.soundcore.com/home"
FILE_URL_PREFIX = "https://ai.soundcore.com/file/"

# Persisted Chromium profile holding the Hub session. Host volume, never in Doppler,
# same rule as rclone.conf and the Plaud CLI token.
PROFILE_DIR = Path(os.environ.get(
    "SOUNDCORE_PROFILE_DIR", str(Path.home() / ".soundcore_profile"))).expanduser()

# Watermark. Deliberately a SEPARATE file from the Plaud pipeline's
# processed_ids.txt: that file is a flat id set and PLAUD_ID_RE assumes Plaud-shaped
# ids, so sharing it would let the two id spaces collide. Maps note id -> version,
# so an edited recording (version moves) is re-pulled instead of being missed.
STATE_DIR = Path(os.environ.get("STATE_DIR", str(Path.home() / ".plaud_pipeline"))).expanduser()
SOUNDCORE_STATE_FILE = STATE_DIR / "soundcore_seen.json"

SOUNDCORE_FORMAT = os.environ.get("SOUNDCORE_FORMAT", "ogg").lower()

# Where --probe writes its findings. Local only, never Drive.
PROBE_DIR = Path(os.environ.get("SOUNDCORE_PROBE_DIR", "/tmp/soundcore_probe"))

# How long --login waits for a human to finish signing in.
LOGIN_TIMEOUT_S = 300

# The Hub renders its file list only after an API round trip, and export re-encodes
# server-side, so both need real waits.
LIST_TIMEOUT_MS = 45_000
DOWNLOAD_TIMEOUT_MS = 120_000


class SoundcoreAuthError(Exception):
    """Raised when the Hub session is gone and a human must sign in again.

    Session expiry is the expected failure mode here, not a rare one, so it gets its
    own exception and its own issue title. A dead session that arrives as a generic
    error is exactly the defect that let free-plaud issue #2 sit unread.
    """


# ------------------------------------------------------------------- state

def load_seen() -> dict[str, str]:
    if not SOUNDCORE_STATE_FILE.exists():
        return {}
    return json.loads(SOUNDCORE_STATE_FILE.read_text(encoding="utf-8"))


def save_seen(seen: dict[str, str]) -> None:
    SOUNDCORE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    SOUNDCORE_STATE_FILE.write_text(json.dumps(seen, indent=2, sort_keys=True),
                                    encoding="utf-8")


def is_new(note: dict, seen: dict[str, str]) -> bool:
    """New, or edited since we last pulled it."""
    prev = seen.get(str(note["id"]))
    return prev is None or prev != str(note.get("version", ""))


# ------------------------------------------------------------------- browser

def open_hub(headless: bool):
    """Launch the persisted profile and land on the Hub. Returns (playwright, ctx, page).

    Raises SoundcoreAuthError if the Hub bounces us to a sign-in screen."""
    from playwright.sync_api import sync_playwright

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    pw = sync_playwright().start()
    ctx = pw.chromium.launch_persistent_context(
        str(PROFILE_DIR),
        headless=headless,
        accept_downloads=True,
        viewport={"width": 1440, "height": 900},
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(HUB_URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(3_000)  # the Hub renders its list client-side
    return pw, ctx, page


def assert_signed_in(page) -> None:
    """Fail loudly and specifically when the session is gone.

    Checked by URL rather than by a 'looks empty' heuristic, because an empty list is
    also what a real account with no recordings looks like, and confusing the two is
    how a dead session gets logged as a quiet night."""
    url = page.url.lower()
    if any(k in url for k in ("login", "signin", "sign-in", "auth")):
        raise SoundcoreAuthError(f"Hub redirected to a sign-in page ({page.url})")


# ------------------------------------------------------- Hub-specific adapter
# Everything that depends on the Hub's markup lives below this line, so a UI change
# is a one-spot fix. These selectors MUST be pinned from a real --probe run rather
# than guessed: the Hub is a closed app with no published DOM contract.


def probe(page) -> dict:
    """Dump enough structure to pin the selectors below, without exporting anything.

    Writes the rendered HTML and a screenshot locally for inspection, and returns a
    summary of repeated elements that look like a file list."""
    try:
        page.wait_for_selector(".file-card", timeout=LIST_TIMEOUT_MS)
    except Exception:
        err("[soundcore] no .file-card appeared; capturing anyway")
    page.wait_for_timeout(1_500)
    PROBE_DIR.mkdir(parents=True, exist_ok=True)
    (PROBE_DIR / "hub.html").write_text(page.content(), encoding="utf-8")
    page.screenshot(path=str(PROBE_DIR / "hub.png"), full_page=True)

    # Find repeated sibling structures: a file manager's rows are the largest group
    # of same-shaped elements on the page.
    summary = page.evaluate("""() => {
        const groups = {};
        document.querySelectorAll('*').forEach(el => {
            if (!el.className || typeof el.className !== 'string') return;
            const key = el.tagName + '.' + el.className.trim().split(/\\s+/).join('.');
            (groups[key] = groups[key] || []).push(el);
        });
        return Object.entries(groups)
            .filter(([, els]) => els.length >= 2)
            .map(([key, els]) => ({
                selector: key,
                count: els.length,
                sample_text: (els[0].innerText || '').slice(0, 120),
            }))
            .sort((a, b) => b.count - a.count)
            .slice(0, 25);
    }""")
    return {"repeated": summary, "url": page.url, "title": page.title()}


# The card's own DOM carries only a title and a datetime, no id, so the note object
# is read out of React's fiber on the card element. Verified against the live Hub:
# the object sits one fiber hop above the card and carries id, app_note_id, title,
# note_md5, audio_full_name, audio_size, audio_duration, version, createTimeStamp
# and updatedTimeStamp.
_NOTES_FROM_FIBER_JS = """() => {
    const out = [];
    for (const card of document.querySelectorAll('.file-card')) {
        const fk = Object.keys(card).find(k => k.startsWith('__reactFiber'));
        if (!fk) continue;
        let found = null;
        const seen = new Set();
        const scan = (o, depth) => {
            if (found || !o || typeof o !== 'object' || depth > 4 || seen.has(o)) return;
            seen.add(o);
            const ks = Object.keys(o);
            if (ks.includes('id') && ks.includes('audio_full_name')) { found = o; return; }
            for (const k of ks) { try { scan(o[k], depth + 1); } catch (e) {} }
        };
        let f = card[fk], up = 0;
        while (f && up < 25 && !found) {
            if (f.memoizedProps) scan(f.memoizedProps, 0);
            if (!found && f.memoizedState) scan(f.memoizedState, 0);
            f = f.return; up++;
        }
        if (found) {
            out.push(JSON.parse(JSON.stringify(found,
                (k, v) => typeof v === 'function' ? undefined : v)));
        }
    }
    return out;
}"""


def list_notes(page) -> list[dict]:
    """Return the Hub's recordings as note dicts (id, title, version, ...).

    Raises rather than returning [] when the list never renders: an empty list is
    also what a real empty account looks like, and treating a broken selector as a
    quiet night is how a fetcher silently does nothing for weeks."""
    page.goto(HUB_URL, wait_until="domcontentloaded", timeout=60_000)
    try:
        # The Hub renders its list client-side after an API round trip, so this is
        # a real wait, not a courtesy one.
        page.wait_for_selector(".file-card", timeout=LIST_TIMEOUT_MS)
    except Exception:
        assert_signed_in(page)  # names the common cause before the generic error
        if _account_is_empty(page):
            return []
        raise RuntimeError(
            "no .file-card rendered and the account does not look empty. The Hub's "
            "markup has probably changed. Run --probe.")
    page.wait_for_timeout(1_500)  # let the last cards attach

    notes = page.evaluate(_NOTES_FROM_FIBER_JS)
    cards = len(page.query_selector_all(".file-card"))
    if len(notes) != cards:
        raise RuntimeError(
            f"read {len(notes)} note object(s) from {cards} card(s). The Hub's "
            "internals have changed and recordings would be skipped. Run --probe.")
    return notes


def _account_is_empty(page) -> bool:
    """True when the sidebar itself reports zero files, e.g. 'All Files (0)'."""
    text = page.evaluate("() => document.body.innerText || ''")
    return bool(re.search(r"All Files\s*\(?\s*0\s*\)?", text))


def export_note(page, note: dict, dest_dir: Path) -> Path:
    """Drive Share -> Export Audio -> format -> Export, and return the saved file.

    Export works even when the recording has no transcript or summary, which is why
    this is the audio path rather than the encrypted HLS player."""
    page.goto(f"{FILE_URL_PREFIX}{note['id']}", wait_until="domcontentloaded",
              timeout=60_000)
    page.wait_for_selector(".ant-dropdown-trigger", timeout=LIST_TIMEOUT_MS)
    page.wait_for_timeout(2_000)

    page.query_selector(".ant-dropdown-trigger").click()
    page.get_by_text("Export Audio", exact=True).click()
    page.wait_for_timeout(1_500)

    # Format is a plain choice in the dialog; OGG is the device's native container,
    # so it is the lossless-passthrough option and AssemblyAI accepts it directly.
    page.get_by_text(SOUNDCORE_FORMAT.upper(), exact=False).first.click()
    page.wait_for_timeout(500)

    with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as dl:
        page.get_by_role("button", name="Export").click()
    download = dl.value
    dest = dest_dir / download.suggested_filename
    download.save_as(str(dest))
    if not dest.exists() or dest.stat().st_size == 0:
        raise RuntimeError(f"export produced an empty file for {note['id']}")
    return dest


# ------------------------------------------------------------------- drive

def upload_to_intake(local_file: Path) -> None:
    """Drop the audio into the intake folder the nightly job already watches.

    Uses the same rclone adapter as the rest of the pipeline. rclone finalises a
    Drive upload only when it completes, so a partial file is never visible to
    process_intake()."""
    _run_rclone([
        "copy", str(local_file), f"{RCLONE_REMOTE}:",
        "--drive-root-folder-id", INTAKE_FOLDER_ID,
    ])


# ------------------------------------------------------------------------- fetch

def fetch_new(dry_run: bool) -> tuple[int, int]:
    """Export every new Hub recording into the Drive intake folder.

    Returns (exported, errors). Session expiry is handled here rather than raised,
    so it files its own named issue whether this runs standalone or as a stage of
    the nightly program, and so one dead session is one counted error instead of an
    exception that aborts the other sources."""
    seen = load_seen()
    ok = errors = 0
    pw = ctx = None
    try:
        pw, ctx, page = open_hub(headless=True)
        assert_signed_in(page)
        notes = list_notes(page)
        new = [n for n in notes if is_new(n, seen)]
        log(f"[soundcore] {len(notes)} recording(s), {len(new)} new")

        for note in new:
            label = note.get("title") or note["id"]
            if dry_run:
                log(f"[soundcore] WOULD export: {label}")
                continue
            log(f"[soundcore] exporting {label}")
            try:
                with tempfile.TemporaryDirectory(prefix="soundcore_") as td:
                    audio = export_note(page, note, Path(td))
                    upload_to_intake(audio)
                # Only after the file is safely in intake, so a failure retries.
                seen[str(note["id"])] = str(note.get("version", ""))
                save_seen(seen)
                ok += 1
                log(f"[soundcore] done {label}")
            except Exception as e:
                errors += 1
                err(f"[soundcore] ERROR {label}: {e}")

    except SoundcoreAuthError as e:
        errors += 1
        err(f"[ALERT][SOUNDCORE_AUTH_FAILED] {e}")
        if not dry_run:
            notify_github_issue(
                "[soundcore-fetch] Hub session expired, re-login needed",
                "The Soundcore fetcher could not reach the Hub with the saved session.\n\n"
                "**Fix:** re-run `python3 cloud/soundcore_fetch.py --login` on the host "
                "and sign in again.\n\n"
                f"- {e}\n- {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    finally:
        if ctx:
            ctx.close()
        if pw:
            pw.stop()
    return ok, errors


# ------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="Soundcore Hub -> Drive intake folder.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--login", action="store_true",
                      help="Open a visible browser and wait for you to sign in once.")
    mode.add_argument("--probe", action="store_true",
                      help="Dump the Hub's structure so selectors can be pinned.")
    mode.add_argument("--dry-run", action="store_true",
                      help="List what WOULD be exported. No downloads, no uploads.")
    args = ap.parse_args()

    if args.login:
        # Headed on purpose: the human signs in, we only persist the result. Waits on
        # the browser reaching a signed-in URL rather than on stdin, so this works the
        # same whether it is launched from a terminal or from a tool with no tty.
        pw, ctx, page = open_hub(headless=False)
        log("[soundcore] sign in to the Hub in the browser window that just opened.")
        log(f"[soundcore] profile: {PROFILE_DIR}")
        try:
            deadline = time.monotonic() + LOGIN_TIMEOUT_S
            while time.monotonic() < deadline:
                try:
                    assert_signed_in(page)
                    log("[soundcore] signed in. Session saved to the profile.")
                    log("[soundcore] next: python3 cloud/soundcore_fetch.py --probe")
                    return 0
                except SoundcoreAuthError:
                    page.wait_for_timeout(2_000)
            err(f"[soundcore] still on a sign-in page after {LOGIN_TIMEOUT_S}s, giving up.")
            return 2
        finally:
            ctx.close()
            pw.stop()

    if args.probe:
        pw, ctx, page = open_hub(headless=False)
        try:
            assert_signed_in(page)
            found = probe(page)
            log(f"[soundcore] url: {found['url']}")
            log(f"[soundcore] wrote {PROBE_DIR}/hub.html and hub.png")
            for row in found["repeated"]:
                log(f"  x{row['count']:<4} {row['selector']}")
                if row["sample_text"].strip():
                    log(f"         sample: {row['sample_text']!r}")
            return 0
        finally:
            ctx.close()
            pw.stop()

    ok, errors = fetch_new(args.dry_run)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
