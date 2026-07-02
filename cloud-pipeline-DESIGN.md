# Plaud → AssemblyAI → Gemini — Cloud Pipeline Design

Automated, repeatable transcription + notes for Plaud recordings (and phone audio),
running unattended in the cloud and writing results to Google Drive.

## 1. Goals & requirements

- **Auto‑fetch** new Plaud recordings (no manual export from the Plaud web UI).
- **Also ingest** phone recordings (Pixel / Google Recorder) dropped into a Drive intake folder.
- **Transcribe** with AssemblyAI (speaker diarization, multilingual).
- **Generate notes** with Gemini `gemini-3.5-flash`, mirroring the Plaud note style
  (title, summary, key points, action items, quotes).
- **Write** transcripts + notes to a specific Google Drive **destination** folder.
- **Run in the cloud**, not dependent on a personal Mac being awake.
- **Idempotent** — never reprocess the same recording.
- **Low‑touch** — only human action is an occasional Plaud re‑login when the token expires.

Out of scope (explicitly): cost guardrails (skipped per owner decision).

## 2. Architecture

A single **GCP `e2‑micro` VM** runs **one nightly job** (systemd timer), co‑located on
the existing free‑tier `teslamate-vm` (project `2026 LLC`) so it costs nothing extra.
The VM runs Container‑Optimized OS, so the pipeline ships as a **Docker container**.
A VM was chosen over GCP serverless because a persistent disk makes the **Plaud CLI's
and rclone's on‑disk tokens a non‑issue** — they auto‑refresh on disk (host volumes)
exactly as on a laptop, with no token write‑back code.

```
Sources                     Engine (e2-micro VM, nightly systemd timer)   Outputs
-------                     -----------------------------------           -------
Plaud Cloud  ──► Plaud CLI ─┐
                            ├─► AssemblyAI ─► Gemini 3.5 Flash ─► Drive: Destination (transcripts + notes)
Drive Intake ──► rclone ────┘                                   └─► Drive: Intake/processed (moved after done)
   ▲                                                            └─► GCP Cloud Logging (run log + errors)
Pixel/Recorder (Share → Drive)
```

The pipeline is **portable** — the same scripts run on a Raspberry Pi or any VPS; nothing
is GCP‑specific except "it happens to run on a GCP VM."

## 3. Components

| Component | Role |
| --- | --- |
| **Plaud CLI** (`@plaud-ai/cli`, Node ≥20) | Auth to the owner's Plaud account; list recent recordings; get 24‑hour audio download URLs. |
| **rclone** (user OAuth) | Drive I/O: list the intake folder, download items, write the destination folder, move processed audio into `processed/`. Authenticated as the account owner (token in `rclone.conf` on a host volume, auto-refreshed). Files are user-owned, so writes to personal-Gmail Drive work — a service account can't (no storage quota). |
| **`plaud_pipeline.py`** (reused verbatim) | The validated transcribe (AssemblyAI) + notes (Gemini) core. Same behavior as the local runs already accepted. |
| **`fetch_and_process`** (new wrapper) | Orchestrates: pull Plaud → scan intake → run pipeline → write/move/log. |
| **systemd timer** | Nightly trigger on the COS VM (portable hosts can use cron instead). |

## 4. Data flow

### 4a. Plaud source
1. `plaud files` / `plaud recent --days 2` → list recording IDs.
2. Skip IDs already in the on‑disk `processed_ids.txt` (persistent disk = reliable state).
3. For each new ID: `plaud file <id>` to confirm `audio` is available → `plaud audio <id>`
   for a 24‑hour URL → download to a **temp dir on the VM**.
4. Run `plaud_pipeline.py` on the temp file → transcript + notes.
5. `rclone copy` transcript + notes to the **destination** Drive folder.
6. Append the ID to `processed_ids.txt`. **Delete the temp audio.**

> Plaud audio is **not** archived to Drive — Plaud Cloud (+ Private Cloud Sync) is the
> system of record. (Configurable: `ARCHIVE_PLAUD_AUDIO=true` also uploads the audio.)

### 4b. Phone / intake source
1. `rclone lsf` the **intake** folder (top-level, non-recursive) for audio files
   (`.m4a/.mp3/.wav/.ogg/...`); the `processed/` subfolder is naturally excluded.
2. For each: `rclone copy` to a temp dir → run `plaud_pipeline.py` → `rclone copy`
   transcript + notes to the **destination** folder.
3. `rclone moveto` the original audio into `Intake/processed/` — this is both the
   archive **and** the "already done" marker (no ID list for intake).

## 5. Idempotency & state
- **Plaud:** on‑disk `processed_ids.txt` (persistent VM disk).
- **Intake:** move‑to‑`processed/` after success.
- A failed file is **not** marked processed, so the next run retries it.

## 6. Auth & token handling
- **Plaud:** one‑time `plaud login` (browser OAuth). Tokens in `~/.plaud/tokens.json`,
  auto‑refreshed/rotated on disk by the CLI.
- **Drive:** **rclone**, one‑time browser OAuth as the account owner. The token lives
  in `rclone.conf` on a host volume, auto‑refreshed in place. Files are user‑owned so
  writes to personal‑Gmail Drive work (a service account has no storage quota and
  cannot create files there — this was tried and fails with `storageQuotaExceeded`).
- **AssemblyAI / Gemini keys:** stored in Doppler, injected via `doppler run` inside
  the container (`DOPPLER_TOKEN` service token on the VM).
- **Token expiry:** when Plaud's refresh token eventually expires (CLI exits with code `2`,
  `AUTH_FAILED`), the job opens a GitHub issue "Plaud login expired" and exits non‑zero.
  Re‑running `plaud login` (or re‑seeding `tokens.json`) is the only recurring manual touch.

## 7. Logging, observability & alerting
- All run output (per‑file status, totals, errors) goes to **stdout/stderr** → the systemd
  service → `journald`. A one‑line summary (`processed=N … errors=M`) ends each run.
- **Two-layer failure alerting:**
  1. **GitHub issue** — on `errors>0` or Plaud auth failure, a deduped issue is opened in
     `GITHUB_REPO` (via a fine-grained `GITHUB_TOKEN`). Catches failures *during* a run.
  2. **Dead-man's-switch** — the job pings a healthchecks.io URL on success; the service
     emails the owner if a ping never arrives (the job **never ran at all** — VM/Docker/
     timer down), which layer 1 structurally cannot detect. `PLAUD_LOOKBACK_DAYS=7`
     means a short outage self-heals without data loss.

## 8. Security
- No inbound services added on the VM. SSH via Google **OS Login / IAP** only.
- API keys live in **Doppler**, injected at runtime via `doppler run` — nothing secret
  in the repo. The container authenticates to Doppler with a `prd`-scoped service token
  (`/etc/plaud-pipeline.env`, chmod 600). The Plaud + rclone tokens are host-volume
  files (chmod 600), auto-refreshed in place.
- The repo (`free-plaud`) contains code only — no keys, audio, or transcripts (already git‑ignored).

## 9. Cost
- **VM:** co‑located on the existing free‑tier `e2‑micro` (`teslamate-vm`) — $0 extra.
- **AssemblyAI:** ~$0.21/hr + ~$0.02/hr diarization (pay‑as‑you‑go; small daily volume).
- **Gemini 3.5 Flash:** free tier covers typical daily note generation.
- **Cloud Logging:** free tier covers this volume.

## 10. Configuration (constants)
- Intake folder ID: `19cuAhqhrWY9BTI2_XZgNQlHWZYu4XTXF`
- Destination folder ID: `1AKFqlZ7RyRXa9Ge_y9221cZzRymjV9jo`
- Notes model: `gemini-3.5-flash`
- Plaud lookback: `--days 2` (overlap guards against missed days)
- Schedule: nightly (e.g., `0 3 * * *` local time)

## 11. One‑time setup (human)
See `cloud/SETUP.md` for the full runbook. In brief:
1. `rclone config` (user OAuth) and `plaud login` — mint both tokens.
2. API keys into Doppler (`prd`); create a `prd` service token for the VM.
3. Seed host volumes on the VM: copy `rclone.conf` + `~/.plaud/tokens.json`; write the
   Doppler token to `/etc/plaud-pipeline.env`.
4. Build the container image on the VM (`docker build`); install the systemd
   service + timer.
5. Confirm one manual run end‑to‑end, then `systemctl enable --now plaud-pipeline.timer`.

## 12. Failure modes
| Failure | Behavior |
| --- | --- |
| Plaud token expired | CLI exit 2 → GitHub issue "Plaud login expired", exit non‑zero, retry next night after re‑login. |
| Job never runs (VM/Docker/timer down) | No healthchecks.io ping → the service emails the owner; a run missed during downtime catches up (`Persistent=true`), and the 7‑day lookback recovers Plaud recordings. |
| One recording errors | Logged; not marked processed; retried next run; others continue. |
| AssemblyAI/Gemini transient error | Per‑file try/except; retried next run. |
| Drive write fails | File not marked processed (intake not moved); retried next run. |
