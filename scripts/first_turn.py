"""Job 1: tokens a harness sends on its first API call, with no task, in an empty repository.

python scripts/first_turn.py --config cc-anthropic codex-openai --runs 3
python scripts/first_turn.py --config cc-anthropic --runs 3 --label 2.1.280 --model claude-sonnet-5   (with CLAUDE_BIN set)
python scripts/first_turn.py --runs 0
"""

import argparse

import summarize
from common import (CONFIGS, PROMPTS, RESULTS, append_csv, check_version, config, harness_version, load_env_file,
                    now_utc, read_csv)
from harnesses import run

OUT = RESULTS / "first_turn"
HEADER = ["config_id", "harness", "harness_version", "model_requested", "model_reported", "route", "effort",
          "run_id", "measured_at", "is_error", "input_uncached", "cache_write", "cache_write_1h", "cache_read",
          "first_turn_context", "output", "tools", "skills", "plugins", "source", "raw"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", nargs="+", default=list(CONFIGS), help="configuration ids (default: all)")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--label", default="", help="suffix for the configuration id; skips the version pin")
    ap.add_argument("--model", default="", help="model id in place of the configuration's (needs --label)")
    ap.add_argument("--timeout", type=int, default=300)
    args = ap.parse_args()
    if args.model and not args.label:
        ap.error("--model needs --label")
    load_env_file()
    prompt = (PROMPTS / "overhead.txt").read_text(encoding="utf-8")
    existing = {r["run_id"] for r in read_csv(OUT / "first_turn.csv")}

    for config_id in args.config if args.runs else []:
        cfg = config(config_id)
        if args.model:
            cfg["model"] = args.model
        label = f"{config_id}@{args.label}" if args.label else config_id
        version = harness_version(cfg["harness"])
        if not args.label:
            check_version(cfg["harness"], version)
        for k in range(1, args.runs + 1):
            run_id = f"{label}-{k}"
            if run_id in existing:
                continue
            print(f"[first turn] {run_id}", flush=True)
            r = run(cfg, "first_turn", prompt, OUT / "raw", run_id, args.timeout)
            first = next((c for c in r["calls"] if c["call_kind"] == "agent"), None) or {}
            env = r["environment"]
            row = {
                "config_id": label, "harness": cfg["harness"], "harness_version": env.get("version") or version,
                "model_requested": cfg["model"], "model_reported": r["model_reported"], "route": cfg["route"],
                "effort": r["effort"], "run_id": run_id, "measured_at": now_utc(), "is_error": r["is_error"],
                **{k2: first.get(k2) for k2 in ("input_uncached", "cache_write", "cache_write_1h", "cache_read", "output")},
                "first_turn_context": sum(first.get(k2, 0) for k2 in ("input_uncached", "cache_write",
                                                                      "cache_write_1h", "cache_read")) if first else None,
                "tools": len(env["tools"]) if env.get("tools") else None,
                "skills": len(env["skills"]) if env.get("skills") else None,
                "plugins": len(env["plugins"]) if env.get("plugins") else None,
                "source": "script", "raw": f"results/first_turn/raw/{run_id}.jsonl",
            }
            append_csv(OUT / "first_turn.csv", HEADER, [row])
            summarize.record_environment(OUT / "environment.json", label, cfg, r)
            print(f"  context {row['first_turn_context']}  error {r['is_error']}", flush=True)
    summarize.first_turn()


if __name__ == "__main__":
    main()
