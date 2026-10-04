"""Job 2: one-line feature request on the fixture HTTP server.

python scripts/measure_session.py --config all --runs 3 --seed 1
python scripts/measure_session.py --config pi-openai --runs 1 --seed 1
"""

import argparse
import random
import sys

import summarize
from common import (CONFIGS, PROMPTS, RESULTS, append_csv, check_version, config, fixture_digest, harness_version,
                    load_env_file, now_utc, read_csv, require_free_port)
from harnesses import run

OUT = RESULTS / "session"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", nargs="+", default=["all"], help="configuration ids, or 'all'")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--timeout", type=int, default=1200)
    args = ap.parse_args()
    load_env_file()
    prompt = (PROMPTS / "task.txt").read_text(encoding="utf-8")
    ids = list(CONFIGS) if args.config == ["all"] else args.config
    done = {r["run_id"] for r in read_csv(OUT / "sessions.csv")}
    order = len(done)
    schedule = [(c, k) for c in ids for k in range(1, args.runs + 1) if f"{c}-{k}" not in done]
    random.Random(args.seed).shuffle(schedule)
    digest = fixture_digest()
    versions = {}

    for config_id, k in schedule:
        cfg = config(config_id)
        if cfg["harness"] not in versions:
            versions[cfg["harness"]] = harness_version(cfg["harness"])
            check_version(cfg["harness"], versions[cfg["harness"]])
        if fixture_digest() != digest:
            sys.exit("error: the fixture changed during collection")
        require_free_port("before the run")
        run_id = f"{config_id}-{k}"
        order += 1
        print(f"[session {order}] {run_id}", flush=True)
        started = now_utc()
        r = run(cfg, "session", prompt, OUT / "raw", run_id, args.timeout)
        r["environment"]["fixture_sha256"] = digest
        calls = [{"config_id": config_id, "run_id": run_id, "call": i, **c} for i, c in enumerate(r["calls"], 1)]
        append_csv(OUT / "per_call.csv", summarize.PER_CALL, calls)
        append_csv(OUT / "sessions.csv", summarize.SESSIONS, [{
            "config_id": config_id, "harness": cfg["harness"],
            "harness_version": r["environment"].get("version") or versions[cfg["harness"]],
            "model": r["model_reported"] or cfg["model"], "route": cfg["route"], "effort": r["effort"],
            "run_id": run_id, "order": order, "started_at": started, "exit_code": r["exit_code"],
            "is_error": r["is_error"], "duration_s": r["duration_s"],
            "api_calls": sum(1 for c in r["calls"] if c["call_kind"] in ("agent", "subagent")),
            "harness_turns": r["harness_turns"], "session_output": r["session_output"],
            "reasoning": r["reasoning"], "harness_reported_cost_usd": r["harness_reported_cost_usd"],
            "added_lines": r["added_lines"], "added_chars": r["added_chars"],
            "used": not (r["is_error"] and r["added_lines"] == 0),
            "exclusion_reason": "error before any edit" if r["is_error"] and r["added_lines"] == 0 else "",
            "raw": f"results/session/raw/{run_id}.jsonl",
        }])
        summarize.record_environment(OUT / "environment.json", config_id, cfg, r)
        print(f"  calls {len(r['calls'])}  output {r['session_output']}  lines {r['added_lines']}  "
              f"{r['duration_s']}s  error {r['is_error']}", flush=True)
        require_free_port("after the run")
    summarize.sessions()


if __name__ == "__main__":
    main()
