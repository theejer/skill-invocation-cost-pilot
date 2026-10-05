import os
import stat
import unittest
from pathlib import Path
from unittest import mock

from support import TempDirCase

import common
import harnesses


class FakeAdapter:
    name = "fake"

    def __init__(self, login, fail_in=None):
        self.login, self.fail_in, self.cleaned = login, fail_in, []

    def command(self, cfg, job, workdir, home):
        common.copy_login(self.login, Path(home) / ".fake" / "login.json")
        if self.fail_in == "command":
            raise SystemExit("error: fixture failure")
        return ["fake-harness"], {}

    def parse(self, cfg, stdout_path, raw_dir, run_id, home):
        if self.fail_in == "parse":
            raise RuntimeError("fixture failure")
        return {"calls": [], "session_output": 0, "reasoning": 0, "is_error": False, "environment": {}}

    def cleanup(self, home):
        path = Path(home) / ".fake" / "login.json"
        self.cleaned.append(path.exists())
        path.unlink(missing_ok=True)


class RunCleanupTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.login = self.tmp / "source-login.json"
        self.login.write_text('{"token": "fixture"}', encoding="utf-8")
        self.root = self.tmp / "runs"
        for target, attr, value in ((common, "RUN_ROOT", self.root),
                                    (harnesses, "run_process", mock.Mock(return_value=(None, True)))):
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_fake(self, fail_in=None):
        adapter = FakeAdapter(self.login, fail_in)
        with mock.patch.dict(harnesses.ADAPTERS, {"fake": adapter}):
            try:
                result = harnesses.run({"harness": "fake"}, "first_turn", "prompt", self.tmp / "raw", "fake-1", 5)
                return adapter, result
            except (SystemExit, RuntimeError) as e:
                return adapter, e

    def test_timeout_cleans_up(self):
        adapter, r = self.run_fake()
        self.assertEqual(r["exit_code"], "timeout")
        self.assertTrue(r["is_error"])
        self.assertEqual(adapter.cleaned, [True])
        self.assertFalse((self.root / "fake-1").exists())
        self.assertTrue(self.login.exists())

    def test_parse_failure_cleans_up(self):
        adapter, r = self.run_fake("parse")
        self.assertIsInstance(r, RuntimeError)
        self.assertEqual(adapter.cleaned, [True])
        self.assertFalse((self.root / "fake-1").exists())

    def test_command_failure_cleans_up(self):
        adapter, r = self.run_fake("command")
        self.assertIsInstance(r, SystemExit)
        self.assertEqual(adapter.cleaned, [True])
        self.assertFalse((self.root / "fake-1").exists())

    def test_stale_run_folder_replaced(self):
        stale = self.root / "fake-1" / "home" / "old.json"
        stale.parent.mkdir(parents=True)
        stale.write_text("{}", encoding="utf-8")
        with common.run_dir("fake-1") as work:
            self.assertEqual(list(work.iterdir()), [])
        self.assertFalse((self.root / "fake-1").exists())


class AdapterCleanupTest(TempDirCase):
    def touch(self, *parts):
        path = self.tmp.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
        return path

    def test_claude(self):
        path = self.touch(".claude", ".credentials.json")
        harnesses.Claude().cleanup(self.tmp)
        self.assertFalse(path.exists())

    def test_codex_closes_relay(self):
        path = self.touch("codex", "auth.json")
        adapter, relay = harnesses.Codex(), mock.Mock()
        adapter.relay = relay
        adapter.cleanup(self.tmp)
        self.assertFalse(path.exists())
        relay.close.assert_called_once()
        self.assertIsNone(adapter.relay)

    def test_codex_closes_relay_when_unlink_fails(self):
        (self.tmp / "codex" / "auth.json").mkdir(parents=True)
        adapter, relay = harnesses.Codex(), mock.Mock()
        adapter.relay = relay
        with self.assertRaises(OSError):
            adapter.cleanup(self.tmp)
        relay.close.assert_called_once()

    def test_opencode_removes_database_and_sidecars(self):
        paths = [self.touch(".local", "share", "opencode", f"opencode.db{s}") for s in ("", "-wal", "-shm", "-journal")]
        harnesses.OpenCode().cleanup(self.tmp)
        self.assertEqual([p.exists() for p in paths], [False] * 4)

    def test_pi_removes_copied_files(self):
        paths = [self.touch(".pi", "agent", n) for n in ("auth.json", "models.json", "settings.json")]
        harnesses.Pi().cleanup(self.tmp)
        self.assertEqual([p.exists() for p in paths], [False] * 3)

    def test_cleanup_without_files(self):
        for adapter in harnesses.ADAPTERS.values():
            adapter.cleanup(self.tmp)


class FileHelpersTest(TempDirCase):
    def test_copy_login(self):
        source = self.tmp / "login.json"
        self.assertFalse(common.copy_login(source, self.tmp / "dest" / "login.json"))
        source.write_text("{}", encoding="utf-8")
        dest = self.tmp / "dest" / "login.json"
        self.assertTrue(common.copy_login(source, dest))
        self.assertEqual(dest.read_text(encoding="utf-8"), "{}")
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o600)

    def test_remove_tree_handles_read_only_files(self):
        tree = self.tmp / "tree"
        locked = tree / "objects" / "ab" / "cdef"
        locked.parent.mkdir(parents=True)
        locked.write_text("x", encoding="utf-8")
        locked.chmod(stat.S_IREAD)
        common.remove_tree(tree)
        self.assertFalse(tree.exists())
        common.remove_tree(tree)

    def test_remove_tree_leaves_link_targets_alone(self):
        outside = self.tmp / "outside.txt"
        outside.write_text("x", encoding="utf-8")
        outside.chmod(stat.S_IREAD)
        mode = outside.stat().st_mode
        tree = self.tmp / "tree"
        tree.mkdir()
        try:
            (tree / "link").symlink_to(outside)
        except OSError:
            self.skipTest("symlinks not available")
        common.remove_tree(tree)
        self.assertFalse(tree.exists())
        self.assertTrue(outside.exists())
        self.assertEqual(outside.stat().st_mode, mode)
        outside.chmod(stat.S_IREAD | stat.S_IWRITE)


if __name__ == "__main__":
    unittest.main()
