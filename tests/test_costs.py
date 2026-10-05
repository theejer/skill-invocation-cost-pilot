import json
import unittest

from support import FIXTURES

from common import added_lines_chars, context_of, input_cost, output_cost, price_for, read_jsonl, spread

PRICES = json.loads((FIXTURES / "prices.json").read_text(encoding="utf-8"))
ANTHROPIC = price_for(PRICES, ("anthropic", "claude-sonnet-5-5"))
OPENAI = price_for(PRICES, ("openai", "gpt-6.1-sol"))


def call(uncached=0, write=0, write_1h=0, read=0):
    return {"input_uncached": str(uncached), "cache_write": str(write), "cache_write_1h": str(write_1h),
            "cache_read": str(read)}


class PriceLookupTest(unittest.TestCase):
    def test_finds_provider_and_model(self):
        self.assertEqual(OPENAI["input"], 1.0)

    def test_unknown_model_raises(self):
        with self.assertRaises(KeyError):
            price_for(PRICES, ("openai", "no-such-model"))


class InputCostTest(unittest.TestCase):
    def test_context_sums_input_buckets(self):
        self.assertEqual(context_of(call(10, 1000, 2000, 5)), 3015)
        blank = {"input_uncached": "", "cache_write": "", "cache_write_1h": "", "cache_read": "7"}
        self.assertEqual(context_of(blank), 7)

    def test_each_bucket_at_its_price(self):
        self.assertAlmostEqual(input_cost(call(10, 1000, 2000, 0), ANTHROPIC), (30 + 3750 + 12000) / 1e6)
        self.assertAlmostEqual(input_cost(call(5, 100, 0, 3000), ANTHROPIC), (15 + 375 + 900) / 1e6)

    def test_blank_counts_are_zero(self):
        self.assertEqual(input_cost({"input_uncached": "", "cache_write": None, "cache_write_1h": "", "cache_read": ""},
                                    OPENAI), 0)

    def test_one_hour_writes_without_price_raise(self):
        with self.assertRaises(ValueError):
            input_cost(call(0, 0, 1, 0), OPENAI)

    def test_output_cost(self):
        self.assertAlmostEqual(output_cost("300", ANTHROPIC), 4500 / 1e6)
        self.assertEqual(output_cost("", ANTHROPIC), 0)


class HelpersTest(unittest.TestCase):
    def test_spread(self):
        self.assertEqual(spread(["1", "", None, "3", "2"]),
                         {"n": 3, "min": 1.0, "median": 2.0, "mean": 2.0, "max": 3.0})
        self.assertEqual(spread([]), {"n": 0})

    def test_added_lines_counts_hunk_additions_only(self):
        diff = ("diff --git a/x.js b/x.js\n--- a/x.js\n+++ b/x.js\n@@ -1,2 +1,3 @@\n a\n+bb\r\n+ccc\n-d\n"
                "diff --git a/y.js b/y.js\nnew file mode 100644\n--- /dev/null\n+++ b/y.js\n@@ -0,0 +1 @@\n+e\n")
        self.assertEqual(added_lines_chars(diff), (3, 6))

    def test_read_jsonl_skips_bad_lines(self):
        self.assertEqual(len(read_jsonl(FIXTURES / "claude" / "direct.jsonl")), 9)
        self.assertEqual(read_jsonl(FIXTURES / "missing.jsonl"), [])


if __name__ == "__main__":
    unittest.main()
