import json
import os
import re
import shutil
import sqlite3
import time
from pathlib import Path

from common import (Timer, added_lines_chars, capture_diff, make_workspace, read_jsonl,
                    resolve_executable, run_process, temp_dir)

OPENROUTER_ANTHROPIC_BASE = "https://openrouter.ai/api"
OPENROUTER_OPENAI_BASE = "https://openrouter.ai/api/v1"


def call_row(kind, model, input_uncached=0, cache_write=0, cache_write_1h=0, cache_read=0,
             output=0, reasoning=0, output_exact=True, thinking=None):
    return {"call_kind": kind, "model_reported": model, "thinking": thinking,
            "input_uncached": int(input_uncached or 0), "cache_write": int(cache_write or 0),
            "cache_write_1h": int(cache_write_1h or 0), "cache_read": int(cache_read or 0),
            "output": int(output or 0), "reasoning": int(reasoning or 0), "output_exact": output_exact}


def openrouter_key():
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit("error: OPENROUTER_API_KEY is not set (put it in .env)")
    return key


# ================================================================ Claude Code

class Claude:
    name = "claude"
    ISOLATION = ["--setting-sources", "project", "--strict-mcp-config", "--no-session-persistence"]
    SESSION = ["--permission-mode", "bypassPermissions"]

    def command(self, cfg, job, workdir, home):
        argv = (resolve_executable("claude") + ["-p", "--model", cfg["model"],
                "--output-format", "stream-json", "--verbose"] + self.ISOLATION
                + (self.SESSION if job == "session" else []))
        env = {"DISABLE_AUTOUPDATER": "1"}
        if cfg["route"] == "openrouter":
            env.update(ANTHROPIC_BASE_URL=OPENROUTER_ANTHROPIC_BASE,
                       ANTHROPIC_AUTH_TOKEN=openrouter_key(), ANTHROPIC_API_KEY="")
        return argv, env

    def cleanup(self, home):
        pass

    @staticmethod
    def _usage(u):
        write_total = u.get("cache_creation_input_tokens") or 0
        split = u.get("cache_creation") or {}
        w1h = split.get("ephemeral_1h_input_tokens") or 0
        w5m = split.get("ephemeral_5m_input_tokens")
        w5m = (write_total - w1h) if w5m is None else w5m
        w5m += max(0, write_total - w5m - w1h)
        return {"input_uncached": u.get("input_tokens") or 0, "cache_write": w5m,
                "cache_write_1h": w1h, "cache_read": u.get("cache_read_input_tokens") or 0,
                "output": u.get("output_tokens") or 0}

    def parse(self, stdout_path, raw_dir, run_id, home):
        events = read_jsonl(stdout_path)
        init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})
        result = next((e for e in reversed(events) if e.get("type") == "result"), {})
        order, usage_by_id, model_by_id, sub_ids, thinking_ids = [], {}, {}, set(), set()
        for e in events:
            msg = e.get("message") if isinstance(e.get("message"), dict) else {}
            mid = msg.get("id")
            if e.get("type") != "assistant" or not mid or not msg.get("usage"):
                continue
            if mid not in usage_by_id:
                order.append(mid)
                if e.get("parent_tool_use_id"):
                    sub_ids.add(mid)
            usage_by_id[mid] = msg["usage"]
            model_by_id[mid] = msg.get("model")
            if any(b.get("type") in ("thinking", "redacted_thinking") for b in msg.get("content") or []):
                thinking_ids.add(mid)
        calls = [call_row("subagent" if mid in sub_ids else "agent", model_by_id.get(mid) or init.get("model"),
                          output_exact=False, thinking=mid in thinking_ids, **self._usage(usage_by_id[mid]))
                 for mid in order]
        iterations = (result.get("usage") or {}).get("iterations") or []
        agent_calls = [c for c in calls if c["call_kind"] == "agent"]
        if iterations and len(iterations) <= len(agent_calls):
            for c, it in zip(agent_calls[len(agent_calls) - len(iterations):], iterations):
                c.update(self._usage(it), output_exact=True)
        usage = result.get("usage") or {}
        models = result.get("modelUsage") or {}
        if len(models) > 1:
            session_output = sum(m.get("outputTokens") or 0 for m in models.values())
        else:
            session_output = usage.get("output_tokens")
        if session_output is None:
            session_output = sum(c["output"] for c in calls)
        reasoning = (usage.get("output_tokens_details") or {}).get("thinking_tokens")
        return {
            "calls": calls,
            "session_output": session_output,
            "reasoning": reasoning,
            "harness_reported_cost_usd": result.get("total_cost_usd"),
            "harness_turns": result.get("num_turns"),
            "model_reported": init.get("model"),
            "effort": init.get("effort") or "harness default",
            "is_error": bool(result.get("is_error")) or not result,
            "environment": {
                "version": init.get("claude_code_version"),
                "tools": init.get("tools"), "skills": init.get("skills"),
                "plugins": [p.get("name") if isinstance(p, dict) else p for p in init.get("plugins") or []],
                "mcp_servers": init.get("mcp_servers"),
                "instruction_files": init.get("memory_paths"),
            },
        }


# ================================================================ Codex

class Codex:
    name = "codex"
    ISOLATION = ["--ignore-user-config", "--ignore-rules", "--skip-git-repo-check"]

    @staticmethod
    def _home(home):
        return Path(home) / "codex"

    def command(self, cfg, job, workdir, home):
        codex_home = self._home(home)
        codex_home.mkdir()
        argv = (resolve_executable("codex") + ["exec", "--json", "-m", cfg["model"]] + self.ISOLATION
                + ["--sandbox", "workspace-write" if job == "session" else "read-only"])
        if cfg["route"] == "openrouter":
            openrouter_key()
            argv += ["-c", 'model_provider="openrouter"',
                     "-c", 'model_providers.openrouter.name="OpenRouter"',
                     "-c", f'model_providers.openrouter.base_url="{OPENROUTER_OPENAI_BASE}"',
                     "-c", 'model_providers.openrouter.env_key="OPENROUTER_API_KEY"',
                     "-c", 'model_providers.openrouter.wire_api="responses"']
        else:
            login = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "auth.json"
            if not login.exists():
                raise SystemExit(f"error: no Codex login at {login}")
            shutil.copyfile(login, codex_home / "auth.json")
        return argv + ["-"], {"CODEX_HOME": str(codex_home)}

    def cleanup(self, home):
        (self._home(home) / "auth.json").unlink(missing_ok=True)

    def _session_file(self, home, thread_id):
        root = self._home(home) / "sessions"
        for _ in range(20):
            hits = sorted(root.rglob(f"rollout-*{thread_id}.jsonl"))
            if hits:
                return hits[-1]
            time.sleep(0.5)
        return None

    @staticmethod
    def _skills(text):
        block = re.search(r"<skills_instructions>(.*?)</skills_instructions>", text, re.S)
        return re.findall(r"^- ([\w.-]+): ", block.group(1), re.M) if block else []

    def parse(self, stdout_path, raw_dir, run_id, home):
        events = read_jsonl(stdout_path)
        thread = next((e.get("thread_id") for e in events if e.get("type") == "thread.started"), None)
        turns = sum(1 for e in events if e.get("type") == "turn.completed")
        failed = any(e.get("type") == "turn.failed" for e in events)
        session = self._session_file(home, thread) if thread else None
        records, meta, ctx, world, skills = [], {}, {}, {}, None
        if session:
            shutil.copyfile(session, raw_dir / f"{run_id}.session.jsonl")
            for e in read_jsonl(session):
                p = e.get("payload") or {}
                if e.get("type") == "session_meta":
                    meta = p
                elif e.get("type") == "turn_context" and not ctx:
                    ctx = p
                elif e.get("type") == "world_state" and not world:
                    world = p.get("state") or {}
                elif e.get("type") == "response_item" and p.get("role") == "developer" and skills is None:
                    skills = self._skills(" ".join(c.get("text", "") for c in p.get("content") or []))
                elif e.get("type") == "token_usage_record":
                    records.append((e.get("ordinal", 0), p))
        seen, calls = set(), []
        model = ctx.get("model")
        for _, p in sorted(records, key=lambda r: r[0]):
            if p.get("response_id") in seen:
                continue
            seen.add(p.get("response_id"))
            u = p.get("usage") or {}
            cached = u.get("cached_input_tokens") or 0
            written = u.get("cache_write_input_tokens") or 0
            calls.append(call_row("agent" if p.get("thread_id") in (None, thread) else "subagent", model,
                                  input_uncached=(u.get("input_tokens") or 0) - cached - written,
                                  cache_write=written, cache_read=cached, output=u.get("output_tokens"),
                                  reasoning=u.get("reasoning_output_tokens"),
                                  thinking=bool(u.get("reasoning_output_tokens"))))
        settings = (ctx.get("collaboration_mode") or {}).get("settings") or {}
        return {
            "calls": calls,
            "session_output": sum(c["output"] for c in calls),
            "reasoning": sum(c["reasoning"] for c in calls),
            "harness_reported_cost_usd": None,
            "harness_turns": turns,
            "model_reported": model,
            "effort": settings.get("reasoning_effort") or "harness default",
            "is_error": failed or not calls,
            "environment": {
                "version": meta.get("cli_version"),
                "model_provider": meta.get("model_provider"),
                "base_instructions_chars": len(((meta.get("base_instructions") or {}).get("text")) or ""),
                "skills": skills,
                "instruction_files": sorted((world.get("agents_md") or {}).keys()),
                "approval_policy": ctx.get("approval_policy"),
                "sandbox": (ctx.get("sandbox_policy") or {}).get("type"),
                "disabled_plugin_ids": ctx.get("disabled_plugin_ids"),
                "session_file_found": bool(session),
            },
        }


# ================================================================ OpenCode

class OpenCode:
    name = "opencode"
    DATA = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")

    def command(self, cfg, job, workdir, home):
        argv = (resolve_executable("opencode") + ["run", "--standalone", "--format", "json", "-m", cfg["model"]]
                + (["--auto"] if job == "session" else []))
        if cfg["route"] == "openrouter":
            openrouter_key()
        return argv, {"HOME": str(home), "USERPROFILE": str(home), "XDG_DATA_HOME": str(self.DATA)}

    def cleanup(self, home):
        pass

    def parse(self, stdout_path, raw_dir, run_id, home):
        events = read_jsonl(stdout_path)
        sid = next((e.get("sessionID") for e in events if e.get("sessionID")), None)
        failed = any(e.get("type") == "error" for e in events)
        db = self.DATA / "opencode" / "opencode.db"
        session, messages = None, []
        if sid and db.exists():
            for _ in range(20):
                con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
                con.row_factory = sqlite3.Row
                session = con.execute("select * from session_v2 where id=?", (sid,)).fetchone()
                messages = con.execute("select type, data from session_message where session_id=? order by seq",
                                       (sid,)).fetchall()
                con.close()
                if session and any(m["type"] == "idle" for m in messages):
                    break
                time.sleep(0.5)
        export = [{"type": m["type"], **json.loads(m["data"])} for m in messages]
        (raw_dir / f"{run_id}.messages.json").write_text(json.dumps(export, indent=1), encoding="utf-8")
        calls, model, variant = [], None, None
        for m in export:
            if m["type"] != "assistant":
                continue
            t = m.get("tokens") or {}
            model = (m.get("model") or {}).get("id") or model
            variant = (m.get("model") or {}).get("variant") or variant
            calls.append(call_row("agent", model, input_uncached=t.get("input"),
                                  cache_write=(t.get("cache") or {}).get("write"),
                                  cache_read=(t.get("cache") or {}).get("read"),
                                  output=(t.get("output") or 0) + (t.get("reasoning") or 0),
                                  reasoning=t.get("reasoning"), thinking=bool(t.get("reasoning"))))
        if session:
            totals = {"input_uncached": session["tokens_input"], "cache_write": session["tokens_cache_write"],
                      "cache_read": session["tokens_cache_read"],
                      "output": (session["tokens_output"] or 0) + (session["tokens_reasoning"] or 0),
                      "reasoning": session["tokens_reasoning"]}
            rest = {k: (v or 0) - sum(c[k] for c in calls) for k, v in totals.items()}
            if any(v > 0 for v in rest.values()):
                calls.append(call_row("other", None, **{k: max(0, v) for k, v in rest.items()}))
        return {
            "calls": calls,
            "session_output": sum(c["output"] for c in calls),
            "reasoning": sum(c["reasoning"] for c in calls),
            "harness_reported_cost_usd": session["cost"] if session else None,
            "harness_turns": sum(1 for m in export if m["type"] == "assistant"),
            "model_reported": model,
            "effort": variant or "harness default",
            "is_error": failed or not calls or any(m["type"] == "idle" and m.get("outcome") != "succeeded"
                                                   for m in export),
            "environment": {"agent": next((m.get("agent") for m in export if m.get("agent")), None)},
        }


# ================================================================ Pi

class Pi:
    name = "pi"
    ISOLATION = ["--no-session", "--no-extensions", "--no-skills", "--no-prompt-templates",
                 "--no-context-files", "--no-approve", "--offline"]

    def command(self, cfg, job, workdir, home):
        argv = resolve_executable("pi") + ["--print", "--mode", "json", "--model", cfg["model"]] + self.ISOLATION
        if cfg["route"] == "openrouter":
            openrouter_key()
        return argv, {"PI_TELEMETRY": "0"}

    def cleanup(self, home):
        pass

    def parse(self, stdout_path, raw_dir, run_id, home):
        events = read_jsonl(stdout_path)
        calls, cost, model, sections = [], 0.0, None, None
        for e in events:
            msg = e.get("message") or {}
            if e.get("type") == "message_start" and msg.get("role") == "system" and sections is None:
                sections = msg.get("sections") or {}
            if e.get("type") == "message_end" and msg.get("role") == "assistant":
                u = msg.get("usage") or {}
                model = msg.get("model") or model
                cost += ((u.get("cost") or {}).get("total") or 0)
                calls.append(call_row("agent", msg.get("model"), input_uncached=u.get("input"),
                                      cache_write=u.get("cacheWrite"), cache_read=u.get("cacheRead"),
                                      output=u.get("output"), reasoning=u.get("reasoning"),
                                      thinking=bool(u.get("reasoning"))))
        stop = next((((e.get("message") or {}).get("stopReason")) for e in reversed(events)
                     if e.get("type") == "message_end"), None)
        sections = sections or {}
        return {
            "calls": calls,
            "session_output": sum(c["output"] for c in calls),
            "reasoning": sum(c["reasoning"] for c in calls),
            "harness_reported_cost_usd": round(cost, 6) if calls else None,
            "harness_turns": sum(1 for e in events if e.get("type") == "turn_end"),
            "model_reported": model,
            "effort": "harness default",
            "is_error": not calls or stop in ("error", "aborted"),
            "environment": {
                "tools": re.findall(r"^- (\w+):", str(sections.get("tools") or ""), re.M) or None,
                "system_prompt_sections": sorted(sections),
            },
        }


ADAPTERS = {a.name: a for a in (Claude(), Codex(), OpenCode(), Pi())}


def run(cfg, job, prompt, raw_dir, run_id, timeout):
    adapter = ADAPTERS[cfg["harness"]]
    raw_dir.mkdir(parents=True, exist_ok=True)
    stdout = raw_dir / f"{run_id}.jsonl"
    with temp_dir() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        try:
            repo = make_workspace(tmp, with_fixture=(job == "session"))
            argv, env = adapter.command(cfg, job, repo, home)
            with Timer() as t:
                code, timed_out = run_process(argv, repo, prompt, stdout, raw_dir / f"{run_id}.stderr.txt",
                                              timeout, env)
            diff = capture_diff(repo)
            parsed = adapter.parse(stdout, raw_dir, run_id, home)
        finally:
            adapter.cleanup(home)
    if job == "session":
        (raw_dir / f"{run_id}.diff").write_text(diff, encoding="utf-8", newline="\n")
    lines, chars = added_lines_chars(diff)
    parsed.update(exit_code="timeout" if timed_out else code, duration_s=t.seconds,
                  added_lines=lines, added_chars=chars)
    parsed["is_error"] = bool(parsed["is_error"] or timed_out or code not in (0, None))
    return parsed
