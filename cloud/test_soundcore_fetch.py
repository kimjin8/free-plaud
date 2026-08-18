#!/usr/bin/env python3
"""
Unit tests for the Soundcore fetcher's non-browser logic.

These run with NO credentials, NO browser and NO network. What is covered is the
part that decides correctness between runs: the watermark (does an edited recording
get re-pulled, does an unchanged one get skipped), the atomic write into the Drive
intake folder, and the session-expiry detection that decides whether a failure is
reported as "re-login needed" or as a nameless error.

The browser-driving functions (list_notes, export_note) are deliberately not mocked
into fake passes here. Mocking Playwright would assert that our mock matches our
code, not that either matches the Hub. Those are verified by running --dry-run and
a real export against the live Hub.

Run:
    python3 -m pytest cloud/test_soundcore_fetch.py -q
"""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "soundcore_fetch", str(Path(__file__).resolve().parent / "soundcore_fetch.py"))
sc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sc)


class FakePage:
    """Minimal stand-in: assert_signed_in only reads .url."""

    def __init__(self, url, body=""):
        self.url = url
        self._body = body

    def evaluate(self, _js):
        return self._body


class TestHelper(unittest.TestCase):
    def patch(self, name, value):
        old = getattr(sc, name)
        setattr(sc, name, value)
        self.addCleanup(lambda: setattr(sc, name, old))

    def setUp(self):
        self._tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.patch("SOUNDCORE_STATE_FILE", self._tmp / "soundcore_seen.json")


class WatermarkTests(TestHelper):
    """The watermark is what stops a nightly job re-exporting everything forever."""

    NOTE = {"id": "abc123", "version": 1786981907158, "title": "2026-08-17 08:49:15"}

    def test_unseen_note_is_new(self):
        self.assertTrue(sc.is_new(self.NOTE, {}))

    def test_same_version_is_not_new(self):
        seen = {"abc123": "1786981907158"}
        self.assertFalse(sc.is_new(self.NOTE, seen))

    def test_edited_note_is_new_again(self):
        # The Hub bumps `version` when a recording is edited. Keying on id alone
        # would silently skip the edit.
        seen = {"abc123": "1786981900000"}
        self.assertTrue(sc.is_new(self.NOTE, seen))

    def test_round_trip_through_disk(self):
        sc.save_seen({"abc123": "1786981907158"})
        self.assertEqual(sc.load_seen(), {"abc123": "1786981907158"})

    def test_missing_state_file_is_empty_not_an_error(self):
        self.assertEqual(sc.load_seen(), {})

    def test_saved_state_is_valid_json(self):
        sc.save_seen({"a": "1", "b": "2"})
        json.loads(sc.SOUNDCORE_STATE_FILE.read_text(encoding="utf-8"))


class SessionExpiryTests(TestHelper):
    """A dead session must name itself instead of arriving as a generic error."""

    def test_signin_redirect_raises_auth_error(self):
        for url in ("https://ai.soundcore.com/login",
                    "https://ai.soundcore.com/signin",
                    "https://passport.anker.com/sign-in?x=1"):
            with self.assertRaises(sc.SoundcoreAuthError):
                sc.assert_signed_in(FakePage(url))

    def test_hub_url_is_accepted(self):
        sc.assert_signed_in(FakePage("https://ai.soundcore.com/home"))
        sc.assert_signed_in(FakePage("https://ai.soundcore.com/file/yg4Abry"))

    def test_empty_account_is_distinguished_from_broken_list(self):
        # An account with zero recordings is legitimate and must not raise. This is
        # the distinction that keeps a real empty listing from looking like a bug,
        # and a broken selector from looking like an empty listing.
        self.assertTrue(sc._account_is_empty(FakePage("u", "All Files\n(0)\nTrash")))
        self.assertFalse(sc._account_is_empty(FakePage("u", "All Files\n(1)\nTrash")))


class IntakeWriteTests(TestHelper):
    """The fetcher must hand off through the same Drive adapter as the nightly job."""

    def test_upload_targets_the_intake_folder(self):
        calls = []
        self.patch("_run_rclone", lambda args: calls.append(args) or "")
        sc.upload_to_intake(Path("/tmp/2026-08-17 08_49_15.ogg"))
        self.assertEqual(len(calls), 1)
        args = calls[0]
        self.assertEqual(args[0], "copy")
        self.assertIn("2026-08-17 08_49_15.ogg", args[1])
        # Must land in the INTAKE folder. Writing to the destination folder instead
        # would publish raw audio into Meeting Log and skip transcription entirely.
        self.assertIn("--drive-root-folder-id", args)
        self.assertEqual(args[args.index("--drive-root-folder-id") + 1],
                         sc.INTAKE_FOLDER_ID)
        self.assertNotEqual(sc.INTAKE_FOLDER_ID, "")


if __name__ == "__main__":
    unittest.main()
