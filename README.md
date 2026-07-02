# Plaud Audio Transcription Pipeline

A simple, self-hosted alternative to Plaud's transcription + notes generation.
Transcribes voice recordings with **AssemblyAI** and generates Plaud-style notes
(title, summary, key points, action items, quotes) with **Google Gemini**.

## How it works

1. `plaud_pipeline.py` uploads each audio file to AssemblyAI, requesting
   multilingual auto-detection and speaker diarization.
2. The resulting transcript is sent to Gemini (`gemini-3.5-flash` by
   default) with a prompt that mirrors Plaud's note style.
3. For each recording it writes two files into `processed/`:
   - `<name>.transcript.txt` — speaker-labeled transcript
   - `<name> - <AI title>.notes.md` — structured notes, auto-named from the content

## Setup

1. Secrets are managed in **Doppler** (project `free-plaud`), not in `.env` files.
   Install the CLI, log in, and select the `dev` config once in this folder:
   ```bash
   brew install dopplerhq/cli/doppler   # or: curl -Ls https://cli.doppler.com/install.sh | sh
   doppler login
   doppler setup -p free-plaud -c dev
   ```
   Add your keys to the Doppler project (dashboard or `doppler secrets set`):
   - `ASSEMBLYAI_API_KEY` — https://www.assemblyai.com/
   - `GEMINI_API_KEY` — Google AI Studio: https://aistudio.google.com/apikey

   See [`cloud/DEPLOY.md`](cloud/DEPLOY.md) for the full variable manifest.

2. (Python deps) The runner auto-installs `requests`. To do it manually:
   ```bash
   python3 -m pip install requests
   ```

## Usage

Drop audio files (`.ogg`, `.mp3`, `.m4a`, `.wav`, ...) into `audio/`, then:

- **Easiest:** double-click `run_batch.command` (macOS).
- **Or from a terminal:**
  ```bash
  python3 plaud_pipeline.py --audio-dir audio --out-dir processed
  # single file:
  python3 plaud_pipeline.py --file "audio/recording.ogg"
  ```

Already-processed files are skipped, so the batch is safe to re-run.

## Notes

- `audio/`, `processed/`, and logs are git-ignored, and API keys live in Doppler —
  your recordings, transcripts, and keys never get committed.
- Override the model with `GEMINI_MODEL` in Doppler (default `gemini-3.5-flash`). The
  script also auto-falls-back to the best available model if the requested one isn't
  found.

## Cloud automation

To run this unattended (no Mac required) — auto-fetching new Plaud recordings via
the Plaud CLI and ingesting audio dropped into a Google Drive intake folder, then
writing transcripts + notes to a Drive destination folder on a nightly cron — see:

- [`cloud-pipeline-DESIGN.md`](cloud-pipeline-DESIGN.md) — architecture and rationale.
- [`cloud/SETUP.md`](cloud/SETUP.md) — copy-paste runbook (GCP free-tier `e2-micro`,
  or any Pi/VPS).

The orchestrator (`cloud/fetch_and_process.py`) reuses this engine verbatim; it adds
the Plaud/Drive I/O, idempotency, and alerting around it.
