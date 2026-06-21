# Plaud Audio Transcription Pipeline

A simple, self-hosted alternative to Plaud's transcription + notes generation.
Transcribes voice recordings with **AssemblyAI** and generates Plaud-style notes
(title, summary, key points, action items, quotes) with **Google Gemini**.

## How it works

1. `plaud_pipeline.py` uploads each audio file to AssemblyAI, requesting
   multilingual auto-detection and speaker diarization.
2. The resulting transcript is sent to Gemini (`gemini-3.1-pro-preview` by
   default) with a prompt that mirrors Plaud's note style.
3. For each recording it writes two files into `processed/`:
   - `<name>.transcript.txt` — speaker-labeled transcript
   - `<name> - <AI title>.notes.md` — structured notes, auto-named from the content

## Setup

1. Copy the env template and add your keys:
   ```bash
   cp .env.example .plaud_env
   # then edit .plaud_env
   ```
   - AssemblyAI key: https://www.assemblyai.com/
   - Gemini key (Google AI Studio): https://aistudio.google.com/apikey

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

- `audio/`, `processed/`, `.plaud_env`, and logs are git-ignored — your
  recordings, transcripts, and API keys never get committed.
- Override the model with `GEMINI_MODEL` in `.plaud_env`. The script also
  auto-falls-back to the best available model if the requested one isn't found.
