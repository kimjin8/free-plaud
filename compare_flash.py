#!/usr/bin/env python3
"""A/B helper: regenerate notes for selected transcripts with a different Gemini
model (default gemini-3.5-flash), using the SAME prompt as the main pipeline so
the only variable is the model. Saves to <base>.notes.<model>.md for comparison.
Does NOT re-transcribe — it reuses the existing transcripts in processed/.
"""
import os
import sys
from pathlib import Path

from plaud_pipeline import gemini_notes, resolve_model  # same dir, same prompt

# The two files to A/B (must already have <base>.transcript.txt in processed/).
BASES = [
    "2026-06-18 18_33_47",   # Pear VC roundtable, 2h9m, English
    "2026-06-05 14_30_19",   # 中国科技品牌海外增长, 1h29m, Chinese
]

def main():
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        sys.exit("ERROR: GEMINI_API_KEY not set.")
    model = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
    model = resolve_model(model, key)
    print(f"Comparison model: {model}")

    proc = Path(__file__).parent / "processed"
    tag = model.replace("gemini-", "").replace("-preview", "")
    for base in BASES:
        tpath = proc / f"{base}.transcript.txt"
        if not tpath.exists():
            print(f"  [skip] no transcript: {base}")
            continue
        transcript = tpath.read_text(encoding="utf-8")
        print(f"  generating notes for {base} ({len(transcript)} chars) ...")
        notes = gemini_notes(transcript, key, model)
        out = proc / f"{base}.notes.{tag}.md"
        out.write_text(notes, encoding="utf-8")
        print(f"  wrote {out.name}")
    print("Done.")

if __name__ == "__main__":
    main()
