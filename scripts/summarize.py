import json

from common import (CONFIGS, RESULTS, context_of, input_cost, load_prices, output_cost, price_for,
                    read_csv, spread, write_csv, write_json)

PER_CALL = ["config_id", "run_id", "call", "call_kind", "model_reported", "thinking", "input_uncached",
            "cache_write", "cache_write_1h", "cache_read", "context", "output", "reasoning", "output_exact",
            "input_cost_usd", "output_cost_usd", "cost_usd"]
SESSIONS = ["config_id", "harness", "harness_version", "model", "route", "effort", "run_id", "order",
            "started_at", "exit_code", "is_error", "duration_s", "api_calls", "harness_turns",
            "final_call_context", "total_input_sent", "token_multiplier", "session_output", "reasoning",
            "session_input_cost_usd", "session_cost_usd", "harness_reported_cost_usd", "final_turn_estimate_usd",
            "added_lines", "added_chars", "used", "exclusion_reason", "raw"]
SPREAD_FIELDS = ["api_calls", "final_call_context", "total_input_sent", "token_multiplier", "session_output",
                 "reasoning", "session_cost_usd", "duration_s", "added_lines"]
POOLED_FIELDS = ["token_multiplier", "api_calls"]
TASK_CALLS = ("agent", "subagent")


def base_config(config_id):
    return CONFIGS[config_id.split("@")[0]]


def record_environment(path, config_id, cfg, result):
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    data.setdefault(config_id, {"harness": cfg["harness"], "model_requested": cfg["model"], "route": cfg["route"],
                                "model_reported": result["model_reported"], "effort": result["effort"],
                                **result["environment"]})
    write_json(path, data)


def first_turn():
    rows = read_csv(RESULTS / "first_turn" / "first_turn.csv")
    out = {}
    for config_id in dict.fromkeys(r["config_id"] for r in rows):
        mine = [r for r in rows if r["config_id"] == config_id and r["is_error"] != "True"]
        if not mine:
            continue
        out[config_id] = {
            "harness": mine[0]["harness"], "harness_version": mine[-1]["harness_version"],
            "model": mine[-1]["model_reported"] or mine[-1]["model_requested"], "route": mine[0]["route"],
            "effort": mine[-1]["effort"], "first_turn_context": spread(r["first_turn_context"] for r in mine),
        }
    if out:
        write_json(RESULTS / "first_turn" / "first_turn.json", out)
    return out


def sessions():
    calls = read_csv(RESULTS / "session" / "per_call.csv")
    runs = read_csv(RESULTS / "session" / "sessions.csv")
    if not runs:
        return
    prices = load_prices()
    warnings = []

    for c in calls:
        p = price_for(prices, base_config(c["config_id"])["price"])
        c["context"] = context_of(c)
        c["input_cost_usd"] = round(input_cost(c, p), 8)
        exact = c["output_exact"] == "True"
        c["output_cost_usd"] = round(output_cost(c["output"], p), 8) if exact else ""
        c["cost_usd"] = round(c["input_cost_usd"] + c["output_cost_usd"], 8) if exact else ""
        if p["long_context_threshold"] and c["context"] > p["long_context_threshold"]:
            warnings.append(f"{c['run_id']} call {c['call']}: context {c['context']} above long-context price threshold")

    for s in runs:
        p = price_for(prices, base_config(s["config_id"])["price"])
        mine = [c for c in calls if c["run_id"] == s["run_id"]]
        agent = [c for c in mine if c["call_kind"] == "agent"]
        if not agent:
            continue
        final = agent[-1]["context"]
        sent = sum(c["context"] for c in mine if c["call_kind"] in TASK_CALLS)
        session_in = sum(c["input_cost_usd"] for c in mine)
        session_cost = session_in + output_cost(s["session_output"], p)
        estimate = (p["input"] * final + p["output"] * int(s["session_output"] or 0)) / 1e6
        s.update(final_call_context=final, total_input_sent=sent,
                 token_multiplier=round(sent / final, 4) if final else "",
                 session_input_cost_usd=round(session_in, 6),
                 session_cost_usd=round(session_cost, 6), final_turn_estimate_usd=round(estimate, 6))
        if any(int(c[k] or 0) < 0 for c in mine for k in ("input_uncached", "cache_write", "cache_read")):
            warnings.append(f"{s['run_id']}: negative token count (check the input mapping)")
        for a, b in zip(agent, agent[1:]):
            if b["context"] < 0.9 * a["context"]:
                warnings.append(f"{s['run_id']}: context drops from {a['context']} to {b['context']} "
                                f"at call {b['call']} (check the input mapping)")
        reported = s["harness_reported_cost_usd"]
        if reported not in ("", None) and float(reported) > 0:
            gap = abs(float(reported) - session_cost) / session_cost
            if gap > 0.01:
                warnings.append(f"{s['run_id']}: harness-reported cost {float(reported):.4f} vs computed "
                                f"{session_cost:.4f} ({gap:.1%})")

    write_csv(RESULTS / "session" / "per_call.csv", PER_CALL, calls)
    write_csv(RESULTS / "session" / "sessions.csv", SESSIONS, runs)

    first = first_turn()
    summary = {}
    for config_id in dict.fromkeys(s["config_id"] for s in runs):
        mine = [s for s in runs if s["config_id"] == config_id]
        used = [s for s in mine if s["used"] == "True" and s["session_cost_usd"] not in ("", None)]
        cfg = base_config(config_id)
        p = price_for(prices, cfg["price"])
        summary[config_id] = {
            "harness": cfg["harness"], "harness_version": mine[-1]["harness_version"], "model": mine[-1]["model"],
            "route": cfg["route"], "effort": mine[-1]["effort"],
            "prices": {k: p[k] for k in ("provider", "model", "input", "output", "cache_write", "cache_write_1h",
                                         "cache_read", "checked")},
            "runs_used": [s["run_id"] for s in used],
            "runs_excluded": [s["run_id"] for s in mine if s not in used],
            **{f: spread(s[f] for s in used) for f in SPREAD_FIELDS},
            "first_turn_context_median": (first.get(config_id) or {}).get("first_turn_context", {}).get("median"),
        }
    pooled = [s for s in runs if s["used"] == "True" and s.get("token_multiplier") not in ("", None)]
    summary = {"pooled": {"configurations": sorted({s["config_id"] for s in pooled}),
                          "runs_used": [s["run_id"] for s in pooled],
                          **{f: spread(s[f] for s in pooled) for f in POOLED_FIELDS}},
               "configurations": summary}
    write_json(RESULTS / "session" / "summary.json", summary)
    for w in warnings:
        print("warning:", w)


if __name__ == "__main__":
    first_turn()
    sessions()
