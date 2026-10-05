import json
import os
import subprocess
import sys
import unittest
from pathlib import Path, PurePosixPath, PureWindowsPath

from support import FIXTURES, REPO, SCRIPTS, TempDirCase

WINDOWS = os.name == "nt"
FAKE_HOME = r"C:\Users\fixtureuser" if WINDOWS else "/home/fixtureuser"
SK_KEY = "sk-" + "fixture" + "0" * 16
ENV_KEY = "fixture-env-key-" + "0123456789"
JWT = "eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12
BEARER = "fixturebearer" + "0123"
GH_TOKEN = "ghp_" + "F" * 36
AWS_KEY = "AKIA" + "F" * 16
SECRETS = [SK_KEY, ENV_KEY, JWT, BEARER, GH_TOKEN, AWS_KEY]


def home_forms(home):
    if WINDOWS:
        p = PureWindowsPath(home)
        tail = "/".join(p.parts[1:])
        return {"HOME": str(p), "HOME_JSON": json.dumps(str(p))[1:-1], "HOME_FWD": p.as_posix(),
                "HOME_GITBASH": f"/{p.drive[0].lower()}/{tail}", "HOME_FLAT": f"{p.drive[0]}--" + "-".join(p.parts[1:])}
    p = PurePosixPath(home)
    return {"HOME": str(p), "HOME_JSON": str(p), "HOME_FWD": str(p), "HOME_GITBASH": str(p),
            "HOME_FLAT": "-".join(p.parts[1:])}


class ScrubCase(TempDirCase):
    def setUp(self):
        super().setUp()
        self.results = self.tmp / "results"
        self.results.mkdir()
        self.tmpdir = self.tmp / "tmp"
        self.tmpdir.mkdir()
        self.runs = self.tmp / "runs"
        self.env = {**os.environ, "USERPROFILE": FAKE_HOME, "HOME": FAKE_HOME, "TMP": str(self.tmpdir),
                    "TEMP": str(self.tmpdir), "TMPDIR": str(self.tmpdir), "PILOT_RESULTS": str(self.results),
                    "PILOT_RUN_ROOT": str(self.runs), "OPENROUTER_API_KEY": ENV_KEY,
                    "ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": "", "OPENAI_API_KEY": "",
                    "PYTHONDONTWRITEBYTECODE": "1"}

    def scrub(self, *args):
        return subprocess.run([sys.executable, str(SCRIPTS / "scrub.py"), *args], env=self.env, cwd=REPO,
                              capture_output=True, text=True, encoding="utf-8")

    def write_raw(self):
        text = (FIXTURES / "scrub" / "raw.jsonl.in").read_text(encoding="utf-8")
        values = {**home_forms(FAKE_HOME), "ROOT": REPO.as_posix(), "TMP": str(self.tmpdir), "RUNS": str(self.runs),
                  "SK_KEY": SK_KEY, "ENV_KEY": ENV_KEY, "JWT": JWT, "BEARER": BEARER, "GH_TOKEN": GH_TOKEN,
                  "AWS_KEY": AWS_KEY}
        for name in sorted(values, key=len, reverse=True):
            text = text.replace("{" + name + "}", values[name])
        path = self.results / "session" / "raw" / "run-1.jsonl"
        path.parent.mkdir(parents=True)
        path.write_bytes(text.encode("utf-8"))
        return path


class ScrubTest(ScrubCase):
    def test_redacts_paths_ids_and_keys(self):
        path = self.write_raw()
        r = self.scrub()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("fixtureuser", text)
        self.assertNotIn(REPO.as_posix(), text)
        self.assertNotIn(str(self.tmpdir), text)
        self.assertNotIn(str(self.runs), text)
        for secret in SECRETS:
            self.assertNotIn(secret, text)
        for token in ("<home>", "<repo>", "<tmp>", "<runs>", "<user>", "<email>", "Bearer <redacted>"):
            self.assertIn(token, text)
        self.assertEqual(text.count("<home>"), 5)
        self.assertIn('"account_id": "<redacted>"', text)
        self.assertIn('"email": "<redacted>"', text)
        self.assertIn('"access_token": "<redacted>"', text)
        self.assertIn("pilot@example.invalid", text)
        self.assertNotIn("person@mail.test", text)
        self.assertIn("1 file(s) scrubbed, 0 user folder(s) remain", r.stdout)

    def test_check_reports_without_writing(self):
        path = self.write_raw()
        before = path.read_bytes()
        r = self.scrub("--check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("1 file(s) to scrub", r.stdout)
        self.assertIn(str(Path("session", "raw", "run-1.jsonl")), r.stdout)
        self.assertEqual(path.read_bytes(), before)

    def test_check_passes_after_scrub(self):
        self.write_raw()
        self.scrub()
        r = self.scrub("--check")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("0 file(s) to scrub, 0 user folder(s) remain", r.stdout)

    def test_idempotent(self):
        path = self.write_raw()
        self.scrub()
        once = path.read_bytes()
        r = self.scrub()
        self.assertEqual(path.read_bytes(), once)
        self.assertIn("0 file(s) scrubbed", r.stdout)

    def test_clean_file_untouched(self):
        path = self.results / "clean.json"
        path.write_bytes(b'{"model": "gpt-6.1-sol", "path": "<repo>/server.js"}\r\n')
        r = self.scrub()
        self.assertEqual(r.returncode, 0)
        self.assertEqual(path.read_bytes(), b'{"model": "gpt-6.1-sol", "path": "<repo>/server.js"}\r\n')

    def test_binary_file_skipped(self):
        (self.results / "blob.bin").write_bytes(b"\xff\xfe\x00\x81")
        r = self.scrub("--check")
        self.assertEqual(r.returncode, 0)
        self.assertIn("skipped (not text)", r.stdout)


class LeftoverTest(ScrubCase):
    def test_other_user_folders_reported(self):
        path = self.results / "notes.txt"
        path.write_text("see D:\\Users\\otherperson\\notes.txt and /Users/otherperson/notes.txt "
                        "and /home/otherperson/notes.txt\n", encoding="utf-8")
        for args in ((), ("--check",)):
            r = self.scrub(*args)
            self.assertEqual(r.returncode, 1)
            self.assertEqual(r.stdout.count("user folder remains"), 3)
            self.assertIn("3 user folder(s) remain", r.stdout)

    def test_urls_and_tokens_not_reported(self):
        (self.results / "ok.txt").write_text("https://example.invalid/home/page <home>/x ~/home/y "
                                             "GET /users/1/posts/2 HTTP/1.1 /HOME/Z\n", encoding="utf-8")
        r = self.scrub("--check")
        self.assertEqual(r.returncode, 0, r.stdout)


if __name__ == "__main__":
    unittest.main()
