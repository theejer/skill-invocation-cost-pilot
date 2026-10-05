import io
import json
import os
import sqlite3
import unittest
import urllib.error
from unittest import mock

from support import FIXTURES, TempDirCase

import harnesses
from common import config

TOKENS = ("input_uncached", "cache_write", "cache_write_1h", "cache_read", "output", "reasoning")


def tokens(call):
    return tuple(call[k] for k in TOKENS)


class ClaudeDirectTest(TempDirCase):
    def parse(self):
        stdout = self.copy_fixture("claude/direct.jsonl", self.tmp / "run.jsonl")
        return harnesses.Claude().parse(config("cc-anthropic"), stdout, self.tmp, "run", self.tmp / "home")

    def test_calls_in_order_with_kinds(self):
        calls = self.parse()["calls"]
        self.assertEqual([c["call_kind"] for c in calls], ["agent", "subagent", "agent", "agent", "unlogged"])

    def test_last_event_of_a_message_wins(self):
        first = self.parse()["calls"][0]
        self.assertEqual(tokens(first), (3, 0, 1000, 0, 40, 0))
        self.assertTrue(first["thinking"])
        self.assertFalse(first["output_exact"])

    def test_cache_write_split(self):
        calls = self.parse()["calls"]
        self.assertEqual((calls[1]["cache_write"], calls[1]["cache_write_1h"]), (300, 0))
        self.assertEqual((calls[2]["cache_write"], calls[2]["cache_write_1h"]), (100, 100))

    def test_iterations_give_exact_final_output(self):
        calls = self.parse()["calls"]
        self.assertEqual(calls[3]["output"], 120)
        self.assertTrue(calls[3]["output_exact"])
        self.assertFalse(calls[2]["output_exact"])

    def test_unlogged_row_holds_session_total_remainder(self):
        unlogged = self.parse()["calls"][-1]
        self.assertEqual(tokens(unlogged), (10, 27, 73, 500, 60, 30))
        self.assertEqual(unlogged["model_reported"], "claude-sonnet-5-5")

    def test_session_totals_and_environment(self):
        r = self.parse()
        self.assertEqual(r["session_output"], 252)
        self.assertEqual(r["reasoning"], 30)
        self.assertEqual(r["harness_reported_cost_usd"], 0.05)
        self.assertEqual(r["harness_turns"], 3)
        self.assertEqual(r["effort"], "medium")
        self.assertFalse(r["is_error"])
        self.assertEqual(r["environment"]["version"], "0.0.1")
        self.assertEqual(r["environment"]["plugins"], ["plugin-a"])
        self.assertEqual(r["environment"]["tools"], ["Read", "Edit", "Bash"])

    def test_missing_result_is_error(self):
        stdout = self.tmp / "run.jsonl"
        lines = (FIXTURES / "claude" / "direct.jsonl").read_text(encoding="utf-8").splitlines()
        stdout.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
        r = harnesses.Claude().parse(config("cc-anthropic"), stdout, self.tmp, "run", self.tmp / "home")
        self.assertTrue(r["is_error"])


class ClaudeOpenRouterTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.generations = json.loads((FIXTURES / "claude" / "generations.json").read_text(encoding="utf-8"))

    def parse(self, lookup):
        stdout = self.copy_fixture("claude/openrouter.jsonl", self.tmp / "run.jsonl")
        with mock.patch.object(harnesses, "openrouter_generation", side_effect=lookup) as m:
            r = harnesses.Claude().parse(config("cc-openai"), stdout, self.tmp, "run", self.tmp / "home")
        return r, m

    def test_calls_take_generation_lookup(self):
        r, m = self.parse(lambda gid: self.generations[gid])
        self.assertEqual([c.args[0] for c in m.call_args_list], ["gen-fixture-0001", "gen-fixture-0002"])
        self.assertEqual([tokens(c) for c in r["calls"]], [(1000, 0, 0, 0, 40, 0), (200, 0, 0, 1500, 60, 10)])
        self.assertEqual([c["billed_cost_usd"] for c in r["calls"]], [0.002, 0.0015])
        self.assertTrue(all(c["output_exact"] for c in r["calls"]))
        self.assertEqual(r["calls"][0]["model_reported"], "openai/gpt-6.1-sol-fixture")

    def test_session_output_from_calls_and_cost_blank(self):
        r, _ = self.parse(lambda gid: self.generations[gid])
        self.assertEqual(r["session_output"], 100)
        self.assertEqual(r["reasoning"], 10)
        self.assertIsNone(r["harness_reported_cost_usd"])
        self.assertFalse(r["is_error"])

    def test_lookups_saved(self):
        self.parse(lambda gid: self.generations[gid])
        saved = json.loads((self.tmp / "run.generations.json").read_text(encoding="utf-8"))
        self.assertEqual([g["id"] for g in saved], ["gen-fixture-0001", "gen-fixture-0002"])
        self.assertNotIn("unused_field", saved[0])

    def test_failed_lookup_is_error(self):
        r, _ = self.parse(lambda gid: None if gid == "gen-fixture-0002" else self.generations[gid])
        self.assertTrue(r["is_error"])
        saved = json.loads((self.tmp / "run.generations.json").read_text(encoding="utf-8"))
        self.assertEqual(len(saved), 1)


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _not_found():
    return urllib.error.HTTPError("https://openrouter.invalid", 404, "Not Found", {}, None)


class OpenRouterLookupTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "fixture-key-0000"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_retries_until_found(self):
        body = json.dumps({"data": {"id": "gen-fixture-0001", "total_cost": 0.5}}).encode()
        with mock.patch("urllib.request.urlopen", side_effect=[_not_found(), _Response(body)]) as m, \
                mock.patch("time.sleep") as sleep:
            g = harnesses.openrouter_generation("gen-fixture-0001", tries=3, wait=0)
        self.assertEqual(g["total_cost"], 0.5)
        self.assertEqual(m.call_count, 2)
        req = m.call_args.args[0]
        self.assertTrue(req.full_url.endswith("/generation?id=gen-fixture-0001"))
        self.assertEqual(req.get_header("Authorization"), "Bearer fixture-key-0000")
        sleep.assert_called_once_with(0)

    def test_gives_up_after_tries(self):
        with mock.patch("urllib.request.urlopen", side_effect=_not_found()), mock.patch("time.sleep"):
            self.assertIsNone(harnesses.openrouter_generation("gen-fixture-0001", tries=2, wait=0))

    def test_other_http_errors_raise(self):
        err = urllib.error.HTTPError("https://openrouter.invalid", 500, "Server Error", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err), mock.patch("time.sleep"):
            with self.assertRaises(urllib.error.HTTPError):
                harnesses.openrouter_generation("gen-fixture-0001", tries=2, wait=0)

    def test_missing_key_stops(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}):
            with self.assertRaises(SystemExit):
                harnesses.openrouter_generation("gen-fixture-0001")


class CodexTest(TempDirCase):
    THREAD = "00000000-0000-4000-8000-0000000c0de1"

    def parse(self, stdout_name="codex/stdout.jsonl", with_session=True):
        home = self.tmp / "home"
        if with_session:
            sessions = home / "codex" / "sessions" / "2026" / "01" / "01"
            self.copy_fixture("codex/session.jsonl", sessions / f"rollout-2026-01-01T00-00-00-{self.THREAD}.jsonl")
        stdout = self.copy_fixture(stdout_name, self.tmp / "run.jsonl")
        with mock.patch("time.sleep"):
            return harnesses.Codex().parse(config("codex-openai"), stdout, self.tmp, "run", home)

    def test_records_sorted_deduplicated_and_split(self):
        calls = self.parse()["calls"]
        self.assertEqual([c["call_kind"] for c in calls], ["agent", "agent", "subagent"])
        self.assertEqual([tokens(c) for c in calls],
                         [(200, 800, 0, 0, 20, 0), (500, 0, 0, 1000, 30, 5), (300, 0, 0, 100, 10, 0)])
        self.assertEqual([c["thinking"] for c in calls], [False, True, False])
        self.assertTrue(all(c["model_reported"] == "gpt-6.1-sol" for c in calls))

    def test_totals_and_environment(self):
        r = self.parse()
        self.assertEqual(r["session_output"], 60)
        self.assertEqual(r["reasoning"], 5)
        self.assertEqual(r["harness_turns"], 1)
        self.assertFalse(r["is_error"])
        env = r["environment"]
        self.assertEqual(env["version"], "0.0.1")
        self.assertEqual(env["effort_reported"], "low")
        self.assertEqual(env["skills"], ["alpha-skill", "beta.skill"])
        self.assertEqual(env["instruction_files"], ["AGENTS.md", "docs/AGENTS.md"])
        self.assertEqual(env["sandbox"], "read-only")
        self.assertEqual(env["approval_policy"], "never")
        self.assertEqual(env["base_instructions_chars"], len("You are a test agent."))
        self.assertTrue(env["session_file_found"])
        self.assertIsNone(env["cache_relay"])

    def test_session_file_copied_to_raw(self):
        self.parse()
        self.assertEqual((self.tmp / "run.session.jsonl").read_bytes(),
                         (FIXTURES / "codex" / "session.jsonl").read_bytes())

    def test_turn_failed_is_error(self):
        stdout = self.tmp / "failed.jsonl"
        stdout.write_text((FIXTURES / "codex" / "stdout.jsonl").read_text(encoding="utf-8")
                          + '{"type": "turn.failed", "error": {"message": "x"}}\n', encoding="utf-8")
        home = self.tmp / "home"
        self.copy_fixture("codex/session.jsonl", home / "codex" / "sessions" / f"rollout-x-{self.THREAD}.jsonl")
        r = harnesses.Codex().parse(config("codex-openai"), stdout, self.tmp, "run", home)
        self.assertTrue(r["is_error"])

    def test_no_session_file_is_error(self):
        r = self.parse(with_session=False)
        self.assertEqual(r["calls"], [])
        self.assertTrue(r["is_error"])
        self.assertFalse(r["environment"]["session_file_found"])


class OpenCodeTest(TempDirCase):
    def build_db(self, home, outcome="succeeded"):
        spec = json.loads((FIXTURES / "opencode" / "db.json").read_text(encoding="utf-8"))
        db = harnesses.OpenCode._data(home) / "opencode.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(db)
        con.execute("create table session_v2 (id text, tokens_input integer, tokens_output integer, "
                    "tokens_reasoning integer, tokens_cache_read integer, tokens_cache_write integer, cost real)")
        con.execute("create table session_message (session_id text, seq integer, type text, data text)")
        s = spec["session"]
        con.execute("insert into session_v2 values (?, ?, ?, ?, ?, ?, ?)",
                    (s["id"], s["tokens_input"], s["tokens_output"], s["tokens_reasoning"],
                     s["tokens_cache_read"], s["tokens_cache_write"], s["cost"]))
        for m in reversed(spec["messages"]):
            data = dict(m["data"])
            if m["type"] == "idle":
                data["outcome"] = outcome
            con.execute("insert into session_message values (?, ?, ?, ?)",
                        (s["id"], m["seq"], m["type"], json.dumps(data)))
        con.commit()
        con.close()

    def parse(self, outcome="succeeded"):
        home = self.tmp / "home"
        self.build_db(home, outcome)
        stdout = self.copy_fixture("opencode/stdout.jsonl", self.tmp / "run.jsonl")
        with mock.patch("time.sleep"):
            return harnesses.OpenCode().parse(config("opencode-anthropic"), stdout, self.tmp, "run", home)

    def test_calls_and_remainder(self):
        calls = self.parse()["calls"]
        self.assertEqual([c["call_kind"] for c in calls], ["agent", "agent", "other"])
        self.assertEqual([tokens(c) for c in calls],
                         [(4, 4000, 0, 5000, 50, 0), (2, 300, 0, 9000, 120, 20), (4, 0, 0, 0, 15, 5)])
        self.assertEqual(calls[0]["model_reported"], "anthropic/claude-sonnet-5.5")
        self.assertIsNone(calls[2]["model_reported"])

    def test_totals_and_environment(self):
        r = self.parse()
        self.assertEqual(r["session_output"], 185)
        self.assertEqual(r["reasoning"], 25)
        self.assertEqual(r["harness_reported_cost_usd"], 0.05)
        self.assertEqual(r["harness_turns"], 2)
        self.assertFalse(r["is_error"])
        self.assertEqual(r["environment"], {"agent": "build", "effort_reported": "medium"})

    def test_messages_exported_in_order(self):
        self.parse()
        export = json.loads((self.tmp / "run.messages.json").read_text(encoding="utf-8"))
        self.assertEqual([m["type"] for m in export], ["user", "assistant", "assistant", "idle"])

    def test_failed_outcome_is_error(self):
        self.assertTrue(self.parse(outcome="failed")["is_error"])


class PiTest(TempDirCase):
    def parse(self, extra=""):
        stdout = self.tmp / "run.jsonl"
        stdout.write_text((FIXTURES / "pi" / "stdout.jsonl").read_text(encoding="utf-8") + extra, encoding="utf-8")
        return harnesses.Pi().parse(config("pi-anthropic"), stdout, self.tmp, "run", self.tmp / "home")

    def test_calls(self):
        calls = self.parse()["calls"]
        self.assertEqual([tokens(c) for c in calls], [(4, 1159, 0, 1190, 57, 0), (2, 88, 0, 2349, 300, 120)])
        self.assertEqual([c["thinking"] for c in calls], [False, True])

    def test_totals_and_environment(self):
        r = self.parse()
        self.assertEqual(r["session_output"], 357)
        self.assertEqual(r["reasoning"], 120)
        self.assertEqual(r["harness_reported_cost_usd"], 0.0077)
        self.assertEqual(r["harness_turns"], 2)
        self.assertEqual(r["model_reported"], "anthropic/claude-sonnet-5.5")
        self.assertFalse(r["is_error"])
        self.assertEqual(r["environment"]["effort_reported"], "medium")
        self.assertEqual(r["environment"]["tools"], ["read", "bash"])
        self.assertEqual(r["environment"]["system_prompt_sections"], ["preamble", "tools"])

    def test_error_stop_is_error(self):
        extra = ('{"type": "message_end", "message": {"role": "assistant", "content": [], "usage": {"input": 1, '
                 '"output": 0, "cacheRead": 0, "cacheWrite": 0, "cost": {"total": 0}}, "stopReason": "error"}}\n')
        self.assertTrue(self.parse(extra)["is_error"])


if __name__ == "__main__":
    unittest.main()
