#!/usr/bin/env python3
"""
Plaud-style pipeline: transcribe audio with AssemblyAI, generate notes with Gemini.

Usage:
    # Process every audio file in a folder (skips ones already done):
    python3 plaud_pipeline.py --audio-dir "/path/to/audio"

    # Process a single file:
    python3 plaud_pipeline.py --file "/path/to/recording.ogg"

Output (written to --out-dir, default: <audio-dir>/processed):
    <basename>.transcript.txt   full transcript (speaker-labeled when available)
    <basename>.notes.md         Plaud-style notes (title, summary, key points, action items)

API keys are read from environment variables:
    ASSEMBLYAI_API_KEY   (required)
    GEMINI_API_KEY       (required)
    GEMINI_MODEL         (optional, default: gemini-3.1-pro-preview)
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

AAI_BASE = "https://api.assemblyai.com/v2"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
AUDIO_EXTS = {".ogg", ".mp3", ".m4a", ".wav", ".opus", ".aac", ".flac", ".mp4"}


# ---------------------------------------------------------------- AssemblyAI

def aai_upload(path: Path, api_key: str) -> str:
    """Upload a local file to AssemblyAI, return the temporary audio_url."""
    headers = {"authorization": api_key}
    with open(path, "rb") as f:
        r = requests.post(f"{AAI_BASE}/upload", headers=headers, data=f)
    r.raise_for_status()
    return r.json()["upload_url"]


def aai_transcribe(audio_url: str, api_key: str) -> dict:
    """Request a transcript and poll until it completes. Returns the transcript JSON."""
    headers = {"authorization": api_key, "content-type": "application/json"}
    payload = {
        "audio_url": audio_url,
        "language_detection": True,       # auto-detect language (handles EN + ZH, etc.)
        "speaker_labels": True,           # diarization
        "punctuate": True,
        "format_text": True,
    }

    def post(p):
        return requests.post(f"{AAI_BASE}/transcript", headers=headers, json=p)

    r = post(payload)
    # Some option combinations aren't valid for every language/account. On a 400,
    # progressively drop the optional fields and retry, surfacing the real reason.
    if r.status_code == 400:
        reason = r.text
        for drop in ("speaker_labels", "language_detection"):
            if drop in payload:
                payload.pop(drop, None)
                r = post(payload)
                if r.status_code < 400:
                    break
        if r.status_code >= 400:
            raise RuntimeError(f"AssemblyAI transcript request failed ({r.status_code}): {reason}")
    r.raise_for_status()
    tid = r.json()["id"]

    while True:
        time.sleep(5)
        s = requests.get(f"{AAI_BASE}/transcript/{tid}", headers=headers)
        s.raise_for_status()
        data = s.json()
        status = data["status"]
        if status == "completed":
            return data
        if status == "error":
            raise RuntimeError(f"AssemblyAI error: {data.get('error')}")
        # else: queued / processing -> keep polling


def transcript_to_text(data: dict) -> str:
    """Render speaker-labeled transcript text when available, else plain text."""
    utterances = data.get("utterances")
    if utterances:
        lines = []
        for u in utterances:
            spk = u.get("speaker", "?")
            lines.append(f"Speaker {spk}: {u['text'].strip()}")
        return "\n\n".join(lines)
    return (data.get("text") or "").strip()


# --------------------------------------------------------------------- Gemini

NOTES_PROMPT = """You are an expert meeting-notes assistant, similar to Plaud AI.
Given a raw transcript of a voice recording, produce clean, well-structured notes.

Write the notes in the SAME primary language as the transcript (e.g. if the
transcript is mostly Chinese, write the notes in Chinese; if English, English).

Output in GitHub-flavored Markdown with EXACTLY these sections:

# <a concise, descriptive title for this recording, max ~12 words>

## Summary
A 3-5 sentence overview of what the recording is about.

## Key Points
- Bulleted, organized list of the main topics and important details discussed.
  Group related points under bold sub-labels where helpful.

## Action Items
- Concrete next steps / to-dos mentioned. If a person is responsible, name them.
- If there are none, write "None identified."

## Notable Quotes
- Up to 3 short, verbatim quotes that capture key moments (omit section if none).

Be faithful to the transcript. Do not invent facts. The first line MUST be the
"# Title" heading because the title will be used to rename the file.

TRANSCRIPT:
---
{transcript}
---
"""


def _gemini_request(method: str, path: str, api_key: str, json_body=None):
    """Call the Gemini REST API trying query-param auth first, then Bearer token.

    Standard AI Studio keys (AIza...) authenticate via ?key=. OAuth-style access
    tokens (AQ.* / ya29.*) authenticate via the Authorization: Bearer header.
    """
    url = f"{GEMINI_BASE}/{path}"
    attempts = [
        ({}, {"key": api_key}),                              # query-param key
        ({"Authorization": f"Bearer {api_key}"}, {}),        # bearer token
    ]
    last = None
    for headers, params in attempts:
        r = requests.request(method, url, headers=headers, params=params,
                             json=json_body, timeout=300)
        if r.status_code < 400:
            return r.json()
        last = r
        if r.status_code not in (401, 403):
            break
    raise RuntimeError(f"Gemini API {r.status_code if last else '?'}: "
                       f"{(last.text if last else '')[:400]}")


def resolve_model(requested: str, api_key: str) -> str:
    """Return a usable model id. If the requested one isn't available, pick the
    best available *pro* model from ListModels (newest-looking first)."""
    try:
        data = _gemini_request("GET", "models", api_key)
    except Exception:
        return requested  # can't list; just try what we were given
    names = [m["name"].split("/")[-1] for m in data.get("models", [])
             if "generateContent" in m.get("supportedGenerationMethods", [])]
    if requested in names:
        return requested
    prefs = ["gemini-3.1-pro-preview", "gemini-3-pro-preview", "gemini-3-pro",
             "gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash"]
    for p in prefs:
        if p in names:
            print(f"  [note] model '{requested}' not found; using '{p}'")
            return p
    pros = sorted([n for n in names if "pro" in n], reverse=True)
    if pros:
        print(f"  [note] model '{requested}' not found; using '{pros[0]}'")
        return pros[0]
    return requested


def gemini_notes(transcript: str, api_key: str, model: str) -> str:
    body = {
        "contents": [{"parts": [{"text": NOTES_PROMPT.format(transcript=transcript)}]}],
        "generationConfig": {"temperature": 0.3},
    }
    data = _gemini_request("POST", f"models/{model}:generateContent", api_key, body)
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError):
        raise RuntimeError(f"Unexpected Gemini response: {json.dumps(data)[:500]}")


# ----------------------------------------------------------------------- main

def slugify_title(notes_md: str) -> str:
    """Pull the '# Title' from the notes for use in the filename."""
    for line in notes_md.splitlines():
        line = line.strip()
        if line.startswith("# "):
            title = line[2:].strip()
            keep = "".join(c if c.isalnum() or c in " -_()&" else "" for c in title)
            return keep.strip()[:80]
    return ""


def process_one(path: Path, out_dir: Path, aai_key: str, gem_key: str, model: str):
    base = path.stem
    transcript_path = out_dir / f"{base}.transcript.txt"
    notes_path = out_dir / f"{base}.notes.md"

    # Notes get auto-renamed to "<base> - <title>.notes.md", so match any variant.
    existing_notes = list(out_dir.glob(f"{base}*.notes.md"))
    if existing_notes and transcript_path.exists():
        print(f"  [skip] already processed: {base}")
        return

    print(f"  [1/3] uploading {path.name} ...")
    audio_url = aai_upload(path, aai_key)

    print(f"  [2/3] transcribing (this can take a while for long files) ...")
    data = aai_transcribe(audio_url, aai_key)
    transcript = transcript_to_text(data)
    transcript_path.write_text(transcript, encoding="utf-8")
    print(f"        transcript saved: {transcript_path.name} ({len(transcript)} chars)")

    print(f"  [3/3] generating notes with {model} ...")
    notes = gemini_notes(transcript, gem_key, model)
    notes_path.write_text(notes, encoding="utf-8")

    title = slugify_title(notes)
    if title:
        nice = out_dir / f"{base} - {title}.notes.md"
        if nice != notes_path and not nice.exists():
            notes_path.rename(nice)
            notes_path = nice
    print(f"        notes saved: {notes_path.name}")


def main():
    ap = argparse.ArgumentParser(description="Transcribe (AssemblyAI) + notes (Gemini).")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--audio-dir", help="Folder of audio files to process.")
    g.add_argument("--file", help="Single audio file to process.")
    ap.add_argument("--out-dir", help="Where to write outputs (default: <audio-dir>/processed).")
    args = ap.parse_args()

    aai_key = os.environ.get("ASSEMBLYAI_API_KEY")
    gem_key = os.environ.get("GEMINI_API_KEY")
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    if not aai_key or not gem_key:
        sys.exit("ERROR: set ASSEMBLYAI_API_KEY and GEMINI_API_KEY environment variables.")

    if args.file:
        files = [Path(args.file)]
        out_dir = Path(args.out_dir) if args.out_dir else files[0].parent / "processed"
    else:
        ad = Path(args.audio_dir)
        files = sorted(p for p in ad.iterdir() if p.suffix.lower() in AUDIO_EXTS)
        out_dir = Path(args.out_dir) if args.out_dir else ad / "processed"

    out_dir.mkdir(parents=True, exist_ok=True)
    model = resolve_model(model, gem_key)
    print(f"Output dir: {out_dir}")
    print(f"Gemini model: {model}")
    print(f"Files to process: {len(files)}")

    for i, path in enumerate(files, 1):
        print(f"\n[{i}/{len(files)}] {path.name}")
        try:
            process_one(path, out_dir, aai_key, gem_key, model)
        except Exception as e:
            print(f"  [ERROR] {path.name}: {e}")

    print("\nDone.")


if __name__ == "__main__":
    main()
