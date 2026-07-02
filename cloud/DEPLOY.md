# Deploy / secrets config — Doppler + GCP

Secrets and config for this repo live in **Doppler**, not in `.env` files. This doc
lists variable **names only** (never values) so prod config can be rebuilt from
scratch. Safe to commit.

## Hosting

- **GCP project:** `2026 LLC` (`gen-lang-client-0680367969`)
- **Host VM:** co-located on the existing free-tier **`teslamate-vm`** (`e2-micro`,
  zone `us-west1-b`). The nightly Plaud job is lightweight (network-bound; the heavy
  transcription runs on AssemblyAI's servers, not the VM), so it shares the box with
  TeslaMate at no extra cost.

## Google Drive access (rclone, user OAuth)

- Drive I/O is done by **rclone authenticated as the account owner** (one-time browser
  OAuth). Files written are owned by the user, so writes to a personal-Gmail Drive
  work. (A service account can't — it has no storage quota.)
- The token lives in `rclone.conf` on the VM host volume
  (`/mnt/stateful_partition/plaud-pipeline/rclone/`), auto-refreshed in place — it is
  **not** a Doppler secret. No folder sharing is needed (it's the user's own Drive).
- rclone remote name: `gdrive` (matches `RCLONE_REMOTE`).

## Doppler project

- **Project:** `free-plaud` (matches the GitHub repo name)
- **Configs:** `dev` (local development), `prd` (the VM nightly cron)

## Variables

| Name | Config(s) | Sensitive | Notes |
|---|---|---|---|
| `ASSEMBLYAI_API_KEY` | dev, prd | ✅ secret | AssemblyAI key |
| `GEMINI_API_KEY` | dev, prd | ✅ secret | Google AI Studio (Gemini) key |
| `GEMINI_MODEL` | dev, prd | no | `gemini-3.5-flash` (the only supported model) |
| `RCLONE_REMOTE` | prd | no | rclone remote name, default `gdrive` |
| `INTAKE_FOLDER_ID` | prd | no | Drive intake folder (has a code default) |
| `DEST_FOLDER_ID` | prd | no | Drive destination folder (has a code default) |
| `PLAUD_LOOKBACK_DAYS` | prd | no | default `2` |
| `ARCHIVE_PLAUD_AUDIO` | prd | no | `true`/`false`, default `false` |
| `STATE_DIR` | prd | no | container uses `/state` (host volume); default `~/.plaud_pipeline` |

Only `*_API_KEY` are Doppler secrets. The Drive (rclone) and Plaud tokens are host-volume
files on the VM, not Doppler secrets. The rest are non-sensitive tunables with safe
code defaults; set them in Doppler only to override.

## Rebuild prod from scratch (names only — safe to commit)

```bash
# 1) API keys into Doppler (prompts on stdin; never pasted on the CLI)
doppler secrets set ASSEMBLYAI_API_KEY -p free-plaud -c prd
doppler secrets set GEMINI_API_KEY     -p free-plaud -c prd
# (repeat for -c dev for local runs)

# 2) Drive token: rclone user OAuth (see cloud/SETUP.md step 1) — token stays in
#    rclone.conf on the VM host volume, never in Doppler.

# 3) Plaud token: `plaud login` (or copy ~/.plaud/tokens.json to the VM host volume).
```

## Running

- **Local:** `doppler run -p free-plaud -c dev -- python3 plaud_pipeline.py --audio-dir audio --out-dir processed`
- **Cloud:** the systemd service runs the container, whose entrypoint is
  `doppler run -p free-plaud -c prd -- python3 fetch_and_process.py` (see `cloud/SETUP.md`).

## VM authentication (prd)

The container authenticates to Doppler with a **service token** scoped to the `prd`
config (`doppler configs tokens create` or the dashboard → Access), stored in
`/etc/plaud-pipeline.env` (chmod 600) and passed in as `DOPPLER_TOKEN`. That token is
the one Doppler credential on the VM; it grants read-only access to the `prd` config,
where the API keys resolve from. (The Drive and Plaud tokens are separate host-volume
files.)
