# skill-invocation-cost-pilot

Cost pilot for a study of skill invocation in LLM coding agents. It measures, for four coding-agent harnesses and two models, how many tokens a harness sends before any task and how many tokens a short feature request uses.

| Job | Measures | Script | Results |
|-----|----------|--------|---------|
| 1. First-turn context | Input tokens on a harness's first API call, with no task | `first_turn.py` | `results/first_turn/` |
| 2. Sessions | Per-call tokens, output tokens, calls and cost for one feature request | `measure_session.py` | `results/session/` |

## Configurations

| `config_id` | Harness | Model | Route |
|---|---|---|---|
| `cc-anthropic` | Claude Code | Claude Sonnet 5.5 | Anthropic (subscription login) |
| `cc-openai` | Claude Code | GPT-6.1 Sol | OpenRouter |
| `codex-openai` | Codex | GPT-6.1 Sol | OpenAI (subscription login) |
| `codex-anthropic` | Codex | Claude Sonnet 5.5 | OpenRouter |
| `opencode-openai` | OpenCode | GPT-6.1 Sol | OpenAI (subscription login) |
| `opencode-anthropic` | OpenCode | Claude Sonnet 5.5 | OpenRouter |
| `pi-openai` | Pi | GPT-6.1 Sol | OpenAI (subscription login) |
| `pi-anthropic` | Pi | Claude Sonnet 5.5 | OpenRouter |

Each run names its model explicitly and sets a fixed effort per model, the default of its vendor's harness: medium for Claude Sonnet 5.5 (Claude Code) and low for GPT-6.1 Sol (Codex). The level is passed as Claude Code `--effort`, Codex `-c model_reasoning_effort=`, OpenCode `-m <model>#<level>` and Pi `--thinking`, and each run records it; `environment.effort_reported` holds the level the harness itself logs (Claude Code logs none). Harness versions are pinned in `scripts/common.py` (Claude Code 2.1.289, Codex 0.160.0, OpenCode 2.0.22, Pi 1.0.0); a run on any other version stops with an error. Subscription-login runs are costed at the same model's API prices.

## Fixture and prompts

- `fixture/http-server/`: a small draft HTTP server in JavaScript (Node.js, no dependencies), used only to estimate cost. It is not the code of any study.
- `prompts/overhead.txt`: the job 1 prompt.
- `prompts/task.txt`: the job 2 feature request.

## Requirements

- Python 3.10 or later (standard library only), git, Node.js.
- The four harnesses at the pinned versions, logged in where the route is a subscription.
- `OPENROUTER_API_KEY` in `.env` for OpenRouter routes.
- On Windows, the scripts run the targets of npm's `.cmd` shims directly; `<NAME>_BIN` (e.g. `CLAUDE_BIN`) overrides the executable.
- Port 8080 free: job 2 stops if it is in use before or after a run.

## Isolation

Each run starts in a new temporary git repository holding one commit: empty for job 1, the fixture for job 2. The prompt is sent on standard input. Every process the harness starts is ended when the run ends. Job 2 checks that the fixture is unchanged before each run and records its SHA-256.

| Harness | Flags and environment | Job 2 permissions |
|---|---|---|
| Claude Code | `-p --setting-sources project --strict-mcp-config --no-session-persistence`; `DISABLE_AUTOUPDATER=1`; `CLAUDE_CODE_SUBAGENT_MODEL` and `ANTHROPIC_DEFAULT_{FABLE,OPUS,SONNET,HAIKU}_MODEL` set to the configuration's model, with `CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1`, so subagents and background calls use it | `--permission-mode bypassPermissions` |
| Codex | `exec --ignore-user-config --ignore-rules --skip-git-repo-check`; the npm package's native `codex.exe` run directly, not through its Node launcher; a new `CODEX_HOME` per run holding only a copy of the login; on OpenRouter's Anthropic models, requests pass through a local relay that adds a top-level `cache_control` field, since those models cache only when asked and Codex cannot add body fields | `--sandbox danger-full-access` |
| OpenCode | `run --standalone`; a new home directory per run | `--auto` |
| Pi | `--print --no-session --no-extensions --no-skills --no-prompt-templates --no-context-files --no-approve --offline` | default tools |

Claude Code still loads its bundled skills and built-in plugins. `environment.json` records what each configuration loaded, where the harness reports it.

## Running

```bash
python scripts/first_turn.py --runs 3
python scripts/measure_session.py --config all --runs 3 --seed 1
python scripts/summarize.py
python scripts/scrub.py
```

`measure_session.py` runs every configuration and run not already recorded, in an order shuffled by `--seed`. `summarize.py` rebuilds every derived value from the CSVs and `prices.json`. `scrub.py` removes local paths, account identifiers and keys from `results/`.

## Definitions

- **Call:** one API call. **Task calls:** calls of kind `agent`, `subagent` or `unlogged`; title generation and other calls outside the task are kind `other`. An `unlogged` row holds the tokens Claude Code reports in its session totals but not per call in its event stream: output the stream reports only at the start of a message, and the calls of a background subagent still running when the session ends. Its tokens are priced at list price.
- **Context of a call** = uncached input + cache write + 1-hour cache write + cache read.
- **First-turn context:** the context of job 1's first call.
- **Final-call context:** the context of the last `agent` call.
- **`total_input_sent`** = Σ context over task calls.
- **`token_multiplier`** = `total_input_sent` ÷ final-call context. It counts tokens only; no price enters it.
- **Input cost of a call** = input × uncached + cache-write price × cache write + 1-hour price × 1-hour write + cache-read price × cache read, with prices from `prices.json`.
- **Session cost** = Σ input cost + output price × session output.
- **Final-turn estimate** = input price × final-call context + output price × session output.

Claude Code reports exact per-call output only for the final call; its session output comes from the session totals, and `output_exact` marks which per-call rows are exact.

Claude Code through OpenRouter (`cc-openai`) reports no per-call token counts. Each call is looked up on OpenRouter's generation endpoint by its generation ID: prompt and cached tokens give the call's context, completion tokens its output, and the cost is OpenRouter's billed `total_cost` (`billed_cost_usd` in `per_call.csv`) rather than a `prices.json` calculation. The lookups are saved as `raw/<run>.generations.json`.

## Results

| File | One row or entry per | Contents |
|---|---|---|
| `first_turn/first_turn.csv` | run | version, model, route, effort, token buckets, first-turn context, counts of tools, skills and plugins |
| `first_turn/first_turn.json` | configuration | spread of first-turn context |
| `session/per_call.csv` | API call | call kind, token buckets, context, output, reasoning, cost, billed cost where looked up |
| `session/sessions.csv` | session | calls, final-call context, total input sent, token multiplier, output, reasoning, costs, lines added, whether used |
| `session/summary.json` | configuration, plus `pooled` | spreads per configuration; token multiplier and calls pooled across all used sessions |
| `*/environment.json` | configuration | what the harness loaded |
| `*/raw/` | run | harness output, stderr, the agent's diff, and Codex's session file, OpenCode's messages or OpenRouter's generation lookups |

A session is excluded (`used` = false) only when an error ended it before any line was added.

## Limits

- One task on one small repository; three sessions per configuration.
- The OpenRouter routes may cache differently from direct provider access.
- Claude Code's prompt cache persists across back-to-back runs, so a run's first call can be partly cached.
- OpenCode does not report which tools or instruction files it loaded.
