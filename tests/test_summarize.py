import contextlib
import io
import json
import shutil
import unittest
from unittest import mock

from support import FIXTURES, TempDirCase

import summarize
from common import read_csv
from measure_session import exclusion


def load_fixture_prices():
    return json.loads((FIXTURES / "prices.json").read_text(encoding="utf-8"))


class SummarizeCase(TempDirCase):
    def setUp(self):
        super().setUp()
        self.results = self.tmp / "results"
        shutil.copytree(FIXTURES / "results", self.results)
        for target, value in (("RESULTS", self.results), ("load_prices", load_fixture_prices)):
            patcher = mock.patch.object(summarize, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_sessions(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            summarize.sessions()
        return out.getvalue()

    def outputs(self):
        names = ["session/per_call.csv", "session/sessions.csv", "session/summary.json", "first_turn/first_turn.json"]
        return {n: (self.results / n).read_bytes() for n in names}

    def sessions(self):
        return {s["run_id"]: s for s in read_csv(self.results / "session" / "sessions.csv")}

    def calls(self):
        return {(c["run_id"], c["call"]): c for c in read_csv(self.results / "session" / "per_call.csv")}

    def summary(self):
        return json.loads((self.results / "session" / "summary.json").read_text(encoding="utf-8"))


class PerCallCostTest(SummarizeCase):
    def test_priced_calls(self):
        self.run_sessions()
        c = self.calls()
        self.assertEqual(c[("cc-anthropic-1", "1")]["context"], "3010")
        self.assertAlmostEqual(float(c[("cc-anthropic-1", "1")]["input_cost_usd"]), 0.01578)
        self.assertAlmostEqual(float(c[("cc-anthropic-1", "2")]["output_cost_usd"]), 0.003)
        self.assertAlmostEqual(float(c[("cc-anthropic-1", "2")]["cost_usd"]), 0.00429)

    def test_inexact_output_left_unpriced(self):
        self.run_sessions()
        first = self.calls()[("cc-anthropic-1", "1")]
        self.assertEqual((first["output_cost_usd"], first["cost_usd"]), ("", ""))

    def test_billed_cost_replaces_price_calculation(self):
        self.run_sessions()
        billed = self.calls()[("cc-openai-1", "2")]
        self.assertEqual((billed["input_cost_usd"], billed["output_cost_usd"]), ("", ""))
        self.assertAlmostEqual(float(billed["cost_usd"]), 0.0015)
        self.assertEqual(billed["context"], "1700")


class SessionCostTest(SummarizeCase):
    def test_priced_session(self):
        self.run_sessions()
        s = self.sessions()["cc-anthropic-1"]
        self.assertEqual(s["final_call_context"], "3105")
        self.assertEqual(s["total_input_sent"], "6615")
        self.assertEqual(s["token_multiplier"], "2.1304")
        self.assertAlmostEqual(float(s["session_input_cost_usd"]), 0.01842)
        self.assertAlmostEqual(float(s["session_cost_usd"]), 0.01842 + 15 * 300 / 1e6)
        self.assertAlmostEqual(float(s["final_turn_estimate_usd"]), (3 * 3105 + 15 * 300) / 1e6)

    def test_billed_session(self):
        self.run_sessions()
        s = self.sessions()["cc-openai-1"]
        self.assertEqual(s["session_input_cost_usd"], "")
        self.assertAlmostEqual(float(s["session_cost_usd"]), 0.0035)
        self.assertAlmostEqual(float(s["final_turn_estimate_usd"]), (1 * 1700 + 8 * 100) / 1e6)
        self.assertEqual(s["token_multiplier"], "1.5882")

    def test_session_without_agent_call_left_blank(self):
        self.run_sessions()
        s = self.sessions()["opencode-openai-1"]
        self.assertEqual((s["final_call_context"], s["session_cost_usd"], s["token_multiplier"]), ("", "", ""))

    def test_warnings(self):
        out = self.run_sessions()
        self.assertIn("warning: codex-openai-1 call 1: context 1200 above long-context price threshold", out)
        self.assertIn("warning: codex-openai-2: harness-reported cost 0.0020 vs computed 0.0010", out)
        self.assertNotIn("cc-anthropic-1: harness-reported", out)


class ExclusionTest(SummarizeCase):
    def test_rule(self):
        self.assertEqual(exclusion(True, 0), (False, "error before any edit"))
        self.assertEqual(exclusion(True, 5), (True, ""))
        self.assertEqual(exclusion(False, 0), (True, ""))
        self.assertEqual(exclusion(False, 5), (True, ""))

    def test_excluded_runs_left_out_of_spreads(self):
        self.run_sessions()
        codex = self.summary()["configurations"]["codex-openai"]
        self.assertEqual(codex["runs_used"], ["codex-openai-2"])
        self.assertEqual(codex["runs_excluded"], ["codex-openai-1"])
        self.assertEqual(codex["api_calls"], {"n": 1, "min": 2.0, "median": 2.0, "mean": 2.0, "max": 2.0})

    def test_run_without_cost_counts_as_excluded(self):
        self.run_sessions()
        opencode = self.summary()["configurations"]["opencode-openai"]
        self.assertEqual((opencode["runs_used"], opencode["runs_excluded"]), ([], ["opencode-openai-1"]))
        self.assertEqual(opencode["session_cost_usd"], {"n": 0})

    def test_pooled_uses_only_used_runs_with_a_multiplier(self):
        self.run_sessions()
        pooled = self.summary()["pooled"]
        self.assertEqual(pooled["configurations"], ["cc-anthropic", "cc-openai", "codex-openai"])
        self.assertEqual(pooled["runs_used"], ["cc-anthropic-1", "cc-openai-1", "codex-openai-2"])
        self.assertEqual(pooled["token_multiplier"],
                         {"n": 3, "min": 1.5882, "median": 2.0, "mean": 1.9062, "max": 2.1304})

    def test_first_turn_errors_excluded(self):
        first = summarize.first_turn()
        self.assertEqual(first["cc-anthropic"]["first_turn_context"],
                         {"n": 3, "min": 19000.0, "median": 19050.0, "mean": 19050.0, "max": 19100.0})
        self.assertEqual(first["pi-openai"]["model"], "openai-codex/gpt-6.1-sol")


class RegenerationTest(SummarizeCase):
    def test_summary_matches_expected(self):
        self.run_sessions()
        expected = json.loads((FIXTURES / "expected" / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(self.summary(), expected)

    def test_summary_carries_prices_and_first_turn(self):
        self.run_sessions()
        cc = self.summary()["configurations"]["cc-anthropic"]
        self.assertEqual(cc["prices"]["input"], 3.0)
        self.assertEqual(cc["first_turn_context_median"], 19050.0)
        self.assertIsNone(self.summary()["configurations"]["codex-openai"]["first_turn_context_median"])

    def test_rerun_on_own_output_is_byte_identical(self):
        self.run_sessions()
        first = self.outputs()
        self.run_sessions()
        self.assertEqual(self.outputs(), first)

    def test_no_sessions_writes_nothing(self):
        (self.results / "session" / "sessions.csv").unlink()
        (self.results / "session" / "per_call.csv").unlink()
        self.run_sessions()
        self.assertFalse((self.results / "session" / "summary.json").exists())


if __name__ == "__main__":
    unittest.main()
