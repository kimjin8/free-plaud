# Cloud pipeline — setup runbook (as-built)

The nightly Plaud + Drive-intake transcription pipeline runs as a **Docker container**
on the existing free-tier **`teslamate-vm`** (`e2-micro`, `us-west1-b`, project
`2026 LLC` / `gen-lang-client-0680367969`), triggered by a **systemd timer**. It costs
nothing extra (shares the box with TeslaMate; the heavy transcription runs on
AssemblyAI's servers, not the VM).

The VM runs **Container-Optimized OS** (no apt/npm on the host), so everything ships
inside the container image. See `../cloud-pipeline-DESIGN.md` for architecture and
`DEPLOY.md` for the secrets/variable manifest.

## Architecture at a glance

```
systemd timer (03:00) -> plaud-pipeline.service -> host-run.sh
   -> docker run plaud-pipeline:latest
        -> doppler run -c prd  (injects ASSEMBLYAI_API_KEY, GEMINI_API_KEY, GEMINI_MODEL)
             -> python fetch_and_process.py
                  -> Plaud CLI (token: host volume)   -> AssemblyAI -> Gemini
                  -> rclone   (user OAuth: host volume) -> Drive: Meeting Log
```

Host state on the stateful partition (`/mnt/stateful_partition/plaud-pipeline/`),
mounted into the container so tokens/state persist and auto-refresh:
- `plaud/`  — Plaud CLI token (`tokens.json`)
- `rclone/` — `rclone.conf` with the **user** OAuth token (files it writes are owned
  by you, so writes to personal-Gmail Drive work — a service account can't, no quota)
- `state/`  — `processed_ids.txt`

## Drive auth — why rclone / user OAuth

A service account has **no Drive storage quota** and cannot create files in a personal
My Drive. So Drive I/O uses **rclone authenticated as you** (one-time browser OAuth).
No folder sharing is needed — it's your own Drive.

---

## Reproduce from scratch

### 1. Mint the rclone token (on any machine with a browser)

```bash
# Never print the config — the token lives inside it.
rclone config create gdrive drive scope drive >/dev/null 2>&1
# Verify read + write without revealing the token:
rclone lsf gdrive: --drive-root-folder-id 19cuAhqhrWY9BTI2_XZgNQlHWZYu4XTXF   # intake
```
The remote name **must** be `gdrive` (matches `RCLONE_REMOTE`). The config file path is
`rclone config file`.

### 2. Secrets in Doppler

API keys live in Doppler (project `free-plaud`, config `prd`); see `DEPLOY.md`. Create a
**service token** scoped to `prd` for the VM.

### 3. Seed host state on the VM

```bash
ZONE=us-west1-b; PROJ=gen-lang-client-0680367969; ROOT=/mnt/stateful_partition/plaud-pipeline
gcloud compute ssh teslamate-vm --zone=$ZONE --project=$PROJ --tunnel-through-iap \
  --command="sudo mkdir -p $ROOT/plaud $ROOT/rclone $ROOT/state && sudo chmod -R 777 $ROOT/plaud $ROOT/state"

# Plaud token (from a machine where you ran `plaud login`) and rclone.conf:
gcloud compute scp ~/.plaud/tokens.json teslamate-vm:$ROOT/plaud/tokens.json --zone=$ZONE --project=$PROJ --tunnel-through-iap
gcloud compute scp "$(rclone config file | tail -1)" teslamate-vm:/tmp/rclone.conf --zone=$ZONE --project=$PROJ --tunnel-through-iap
gcloud compute ssh teslamate-vm --zone=$ZONE --project=$PROJ --tunnel-through-iap \
  --command="sudo cp /tmp/rclone.conf $ROOT/rclone/rclone.conf && sudo chmod 600 $ROOT/rclone/rclone.conf"
```

### 4. Doppler service token -> /etc/plaud-pipeline.env (never printed)

```bash
doppler configs tokens create teslamate-vm --project free-plaud --config prd --plain 2>/dev/null \
  | sed 's/^/DOPPLER_TOKEN=/' \
  | gcloud compute ssh teslamate-vm --zone=$ZONE --project=$PROJ --tunnel-through-iap \
      --command="sudo tee /etc/plaud-pipeline.env >/dev/null && sudo chmod 600 /etc/plaud-pipeline.env"
```

### 5. Build the image + install systemd units (on the VM)

Ship `plaud_pipeline.py` + `cloud/` to the VM (`gcloud compute scp` a tarball), then:

```bash
cd <build-context>
sudo docker build -f cloud/Dockerfile -t plaud-pipeline:latest .
sudo cp cloud/host-run.sh $ROOT/host-run.sh
sudo cp cloud/systemd/plaud-pipeline.service /etc/systemd/system/
sudo cp cloud/systemd/plaud-pipeline.timer   /etc/systemd/system/
sudo systemctl daemon-reload
```

> COS mounts the stateful partition **noexec**, so the service runs the script via
> `bash` (already set in the unit). Build natively on the VM (amd64) — no registry
> needed; `/mnt/stateful_partition` has ~19 GB free.

### 6. Test, then enable

```bash
# Dry-run (no paid calls):
sudo bash -c "set -a; . /etc/plaud-pipeline.env; set +a; bash $ROOT/host-run.sh --dry-run"
# Real run of just Plaud (small):
sudo bash -c "set -a; . /etc/plaud-pipeline.env; set +a; bash $ROOT/host-run.sh --source plaud"
# Enable nightly:
sudo systemctl enable --now plaud-pipeline.timer
systemctl list-timers plaud-pipeline.timer --no-pager
```

The timer fires `OnCalendar=*-*-* 03:00:00` in the VM's timezone (UTC by default —
`sudo timedatectl set-timezone ...` to change).

---

## Failure alerting (two layers)

**1. GitHub issue on failure.** On an errored run or Plaud auth failure, the pipeline
opens a deduped issue in `GITHUB_REPO` using `GITHUB_TOKEN` (fine-grained PAT scoped to
`free-plaud`, Issues read/write). Set both in Doppler `prd`.

**2. Dead-man's-switch (healthchecks.io)** — catches the case layer 1 can't: the job
*never running at all* (VM down, Docker/timer broken). The pipeline pings
`HEALTHCHECK_URL` on success (`…/fail` on failure); healthchecks.io emails you if a ping
is ever missing.
- Create a check (Simple schedule, period 1 day, grace ~4 hours) with an email
  integration; copy its ping URL into Doppler `prd` as `HEALTHCHECK_URL`.
- On a Plaud auth failure you'll get the GitHub issue "Plaud login expired" — re-run
  `plaud login` (or re-seed `tokens.json`).

> Also: `PLAUD_LOOKBACK_DAYS=7` in prd means an outage up to a week self-heals on
> recovery (idempotency skips already-processed recordings), so a short miss loses
> nothing even before you react to an alert.

## Day-2 operations

- **Logs:** `journalctl -u plaud-pipeline.service --since today` (or Cloud Logging).
- **Manual run:** `sudo systemctl start plaud-pipeline.service` (full run, all sources).
- **Upgrade Plaud CLI / rebuild:** re-ship `cloud/` + `sudo docker build ...` again. The
  CLI updates often; only the "plaud CLI adapter" in `fetch_and_process.py` would need
  changes if its output format shifts.
- **Force re-process:** remove a Plaud ID from `state/processed_ids.txt`; for intake,
  move a file back into the top level of the intake folder.
- **Archive Plaud audio too:** set `ARCHIVE_PLAUD_AUDIO=true` in Doppler `prd`.
- **rclone token:** auto-refreshes in `rclone/rclone.conf`. If it's ever revoked,
  re-run step 1 and re-copy the config (step 3).
