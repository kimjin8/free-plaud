#!/usr/bin/env python3
"""
Unit + orchestration tests for the cloud pipeline.

These run with NO credentials and make NO network / paid-API calls: every external
boundary (the `plaud` CLI, `rclone`, AssemblyAI, Gemini, and the HTTP download) is
mocked, so the full orchestration logic — parsing, idempotency, per-item failure
isolation, dry-run, and the auth-failure alert path — is exercised end to end.

Run:
    python3 cloud/test_fetch_and_process.py
    # or: python3 -m pytest cloud/test_fetch_and_process.py
"""

import importlib.util
import io
import sys
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

# Import the orchestrator module directly (it isn't a package).
_SPEC = importlib.util.spec_from_file_location(
    "fetch_and_process", str(Path(__file__).resolve().parent / "fetch_and_process.py"))
fp = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(fp)


# Canned `plaud recent` output (two leading spaces per data row, as the real CLI emits).
RECENT_OUTPUT = """
Recordings in the last 2 days: 3

  rec_alpha000000001  Standup meeting        2026-06-23  12m30s
  rec_bravo000000002  Vendor call            2026-06-24  3m05s
  rec_charlie00000003  One on one             2026-06-24  45m00s
"""

RECENT_EMPTY = "No recordings in the last 2 days.\n"

AUDIO_OUTPUT_TMPL = """
Audio Download URL:

https://plaud-presigned.example.com/{rid}.mp3?sig=abc123

Note: This URL expires in 24 hours.
"""

AUDIO_UNAVAILABLE = "Audio not available for this recording.\n"


class TestHelper(unittest.TestCase):
    def patch(self, name, value):
        """Set fp.<name> = value for the duration of one test, then restore."""
        old = getattr(fp, name)
        setattr(fp, name, value)
        self.addCleanup(lambda: setattr(fp, name, old))

    def setUp(self):
        # Redirect state to a temp dir so tests never touch the real disk.
        self._tmp = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))
        self.patch("STATE_DIR", self._tmp)
        self.patch("PROCESSED_IDS_FILE", self._tmp / "processed_ids.txt")
        # Records of what got copied to the destination / moved to processed/.
        self.copied = []
        self.moved = []
        self.patch("rclone_copy_to_dest", lambda f: self.copied.append(Path(f).name))
        self.patch("rclone_move_intake_to_processed", lambda n: self.moved.append(n))
        # Stub the engine so no network/paid calls happen.
        self.patch("aai_upload", lambda path, key: "fake://upload")
        self.patch("aai_transcribe", lambda url, key: {"text": "hello world"})
        # transcript_to_text and slugify_title are pure — keep the real ones.
        self.patch("gemini_notes",
                   lambda transcript, key, model: "# Demo Title\n\n## Summary\nok.")
        self.patch("_download", lambda url, dest: Path(dest).write_bytes(b"AUDIO"))


class ParsingTests(TestHelper):
    def test_recent_ids_parsed(self):
        self.patch("_run_plaud", lambda args: RECENT_OUTPUT)
        ids = fp.plaud_recent_ids(2)
        self.assertEqual(
            ids, ["rec_alpha000000001", "rec_bravo000000002", "rec_charlie00000003"])

    def test_recent_summary_line_not_mistaken_for_id(self):
        self.patch("_run_plaud", lambda args: RECENT_OUTPUT)
        self.assertNotIn("Recordings", fp.plaud_recent_ids(2))

    def test_recent_empty(self):
        self.patch("_run_plaud", lambda args: RECENT_EMPTY)
        self.assertEqual(fp.plaud_recent_ids(2), [])

    def test_audio_url_extracted(self):
        self.patch("_run_plaud", lambda args: AUDIO_OUTPUT_TMPL.format(rid="rec_x"))
        self.assertEqual(
            fp.plaud_audio_url("rec_x"),
            "https://plaud-presigned.example.com/rec_x.mp3?sig=abc123")

    def test_audio_unavailable_returns_none(self):
        self.patch("_run_plaud", lambda args: AUDIO_UNAVAILABLE)
        self.assertIsNone(fp.plaud_audio_url("rec_x"))

    def test_intake_filters_to_audio_only(self):
        listing = "Standup.m4a\nNotes.txt\ncall.mp3\nimage.png\narchive.zip\nvoice.opus\n"
        self.patch("_run_rclone", lambda args: listing)
        self.assertEqual(fp.rclone_intake_files(), ["Standup.m4a", "call.mp3", "voice.opus"])


class PlaudOrchestrationTests(TestHelper):
    def _plaud_dispatch(self, raise_on=None):
        def runner(args):
            if args[0] == "recent":
                return RECENT_OUTPUT
            if args[0] == "audio":
                return AUDIO_OUTPUT_TMPL.format(rid=args[1])
            return ""
        if raise_on:
            real_dl = fp._download
            def dl(url, dest):
                if raise_on in url:
                    raise RuntimeError("simulated download failure")
                return real_dl(url, dest)
            self.patch("_download", dl)
        return runner

    def test_processes_new_and_skips_known(self):
        self.patch("_run_plaud", self._plaud_dispatch())
        # alpha already processed -> should be skipped.
        fp.mark_processed_id("rec_alpha000000001")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            ok, errors = fp.process_plaud(
                fp.load_processed_ids(), "k", "k", "gemini-3.5-flash", dry_run=False)
        self.assertEqual((ok, errors), (2, 0))
        done = fp.load_processed_ids()
        self.assertIn("rec_bravo000000002", done)
        self.assertIn("rec_charlie00000003", done)
        # Two outputs (transcript + notes) per processed recording = 4 copies.
        self.assertEqual(len(self.copied), 4)

    def test_failure_isolation_does_not_mark_failed(self):
        self.patch("_run_plaud", self._plaud_dispatch(raise_on="rec_bravo000000002"))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            ok, errors = fp.process_plaud(
                set(), "k", "k", "gemini-3.5-flash", dry_run=False)
        self.assertEqual((ok, errors), (2, 1))  # alpha+charlie ok, bravo failed
        done = fp.load_processed_ids()
        self.assertNotIn("rec_bravo000000002", done)   # failed item retries next run
        self.assertIn("rec_alpha000000001", done)
        self.assertIn("rec_charlie00000003", done)

    def test_dry_run_makes_no_side_effects(self):
        self.patch("_run_plaud", self._plaud_dispatch())
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            ok, errors = fp.process_plaud(
                set(), "k", "k", "gemini-3.5-flash", dry_run=True)
        self.assertEqual((ok, errors), (0, 0))
        self.assertEqual(self.copied, [])
        self.assertEqual(fp.load_processed_ids(), set())


class IntakeOrchestrationTests(TestHelper):
    def setUp(self):
        super().setUp()
        listing = "meeting.m4a\nnote.txt\ncall.mp3\n"
        self.patch("_run_rclone", lambda args: listing)
        self.patch("rclone_copy_from_intake",
                   lambda name, d: Path(d).joinpath(name).write_bytes(b"A") or Path(d) / name)

    def test_intake_processes_and_moves(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            ok, errors = fp.process_intake("k", "k", "gemini-3.5-flash", dry_run=False)
        self.assertEqual((ok, errors), (2, 0))                    # two audio files
        self.assertEqual(sorted(self.moved), ["call.mp3", "meeting.m4a"])
        self.assertEqual(len(self.copied), 4)                     # 2 outputs x 2 files

    def test_intake_dry_run_moves_nothing(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            ok, errors = fp.process_intake("k", "k", "gemini-3.5-flash", dry_run=True)
        self.assertEqual((ok, errors), (0, 0))
        self.assertEqual(self.moved, [])


class AuthAlertTests(TestHelper):
    def test_auth_failure_emits_alert_and_exits_2(self):
        def boom(args):
            raise fp.PlaudAuthError("AUTH_FAILED")
        self.patch("_run_plaud", boom)
        self.patch("plaud_version", lambda: "test")
        # Skip intake for this test.
        self.patch("rclone_intake_files", lambda: [])
        # Capture GitHub-issue notifications instead of hitting the network.
        self.notified = []
        self.patch("notify_github_issue", lambda t, b: self.notified.append(t))
        argv = sys.argv
        sys.argv = ["fetch_and_process.py", "--source", "plaud"]
        self.addCleanup(lambda: setattr(sys, "argv", argv))
        out, errbuf = io.StringIO(), io.StringIO()
        # which() must find the tools; pretend they exist.
        self.patch("shutil", type("S", (), {"which": staticmethod(lambda t: "/usr/bin/" + t)}))
        with redirect_stdout(out), redirect_stderr(errbuf):
            # keys present so we don't bail early
            import os
            os.environ["ASSEMBLYAI_API_KEY"] = "x"
            os.environ["GEMINI_API_KEY"] = "x"
            self.patch("resolve_model", lambda m, k: m)
            rc = fp.main()
        self.assertEqual(rc, 2)
        self.assertIn("[ALERT][PLAUD_AUTH_FAILED]", errbuf.getvalue())
        self.assertIn("processed=0", out.getvalue())
        # A GitHub issue is filed on auth failure.
        self.assertEqual(self.notified,
                         ["[plaud-pipeline] Plaud login expired — re-auth needed"])


class GitHubNotifyTests(TestHelper):
    def test_noop_when_unconfigured(self):
        # No GITHUB_TOKEN/REPO in the test env -> must not touch the network.
        self.patch("GITHUB_TOKEN", "")
        self.patch("GITHUB_REPO", "")
        def explode(*a, **k):
            raise AssertionError("network called despite missing GitHub config")
        self.patch("requests", type("R", (), {"get": staticmethod(explode),
                                               "post": staticmethod(explode)}))
        with redirect_stderr(io.StringIO()):
            fp.notify_github_issue("t", "b")  # should simply return

    def test_dedupes_open_issue(self):
        self.patch("GITHUB_TOKEN", "tok")
        self.patch("GITHUB_REPO", "owner/repo")
        posted = []
        class Resp:
            ok = True
            def __init__(self, data): self._d = data
            def json(self): return self._d
        def fake_get(url, **k): return Resp([{"title": "dup-title"}])
        def fake_post(url, **k): posted.append(k.get("json", {}).get("title")); return Resp({"number": 1})
        self.patch("requests", type("R", (), {"get": staticmethod(fake_get),
                                               "post": staticmethod(fake_post)}))
        with redirect_stderr(io.StringIO()):
            fp.notify_github_issue("dup-title", "body")  # open issue exists -> skip POST
        self.assertEqual(posted, [])  # deduped, no new issue


if __name__ == "__main__":
    unittest.main(verbosity=2)
