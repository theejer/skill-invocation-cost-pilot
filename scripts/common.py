import csv
import hashlib
import json
import os
import re
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = Path(os.environ.get("PILOT_RESULTS") or ROOT / "results")
FIXTURE = ROOT / "fixture" / "http-server"
FIXTURE_PORT = 8080
PROMPTS = ROOT / "prompts"

ANTHROPIC_MODEL = "claude-sonnet-5-5"
OPENAI_MODEL = "gpt-6.1-sol"
OPENROUTER_ANTHROPIC = "anthropic/claude-sonnet-5.5"
OPENROUTER_OPENAI = "openai/gpt-6.1-sol"

CONFIGS = {
    "cc-anthropic":       {"harness": "claude",   "model": ANTHROPIC_MODEL,                      "route": "direct",     "price": ("anthropic", ANTHROPIC_MODEL)},
    "cc-openai":          {"harness": "claude",   "model": OPENROUTER_OPENAI,                    "route": "openrouter", "price": ("openrouter", OPENROUTER_OPENAI)},
    "codex-openai":       {"harness": "codex",    "model": OPENAI_MODEL,                         "route": "direct",     "price": ("openai", OPENAI_MODEL)},
    "codex-anthropic":    {"harness": "codex",    "model": OPENROUTER_ANTHROPIC,                 "route": "openrouter", "price": ("openrouter", OPENROUTER_ANTHROPIC)},
    "opencode-openai":    {"harness": "opencode", "model": f"openai/{OPENAI_MODEL}",             "route": "direct",     "price": ("openai", OPENAI_MODEL)},
    "opencode-anthropic": {"harness": "opencode", "model": f"openrouter/{OPENROUTER_ANTHROPIC}", "route": "openrouter", "price": ("openrouter", OPENROUTER_ANTHROPIC)},
    "pi-openai":          {"harness": "pi",       "model": f"openai-codex/{OPENAI_MODEL}",       "route": "direct",     "price": ("openai", OPENAI_MODEL)},
    "pi-anthropic":       {"harness": "pi",       "model": f"openrouter/{OPENROUTER_ANTHROPIC}", "route": "openrouter", "price": ("openrouter", OPENROUTER_ANTHROPIC)},
}

PINNED_VERSIONS = {"claude": "2.1.288", "codex": "0.160.0", "opencode": "2.0.22", "pi": "1.0.0"}

TOKEN_FIELDS = ["input_uncached", "cache_write", "cache_write_1h", "cache_read", "output", "reasoning"]

GIT_ID = ["-c", "user.name=pilot", "-c", "user.email=pilot@example.invalid",
          "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false"]


def config(config_id):
    if config_id not in CONFIGS:
        sys.exit(f"error: unknown configuration '{config_id}' (known: {', '.join(CONFIGS)})")
    return {"config_id": config_id, **CONFIGS[config_id]}


def load_env_file():
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


# ---------------------------------------------------------------- executables and versions

def resolve_executable(name):
    override = os.environ.get(f"{name.upper()}_BIN")
    if override:
        return [override]
    found = shutil.which(name)
    if not found:
        sys.exit(f"error: '{name}' is not on PATH (set {name.upper()}_BIN to override)")
    if found.lower().endswith((".cmd", ".bat")):
        text = Path(found).read_text(encoding="utf-8", errors="replace")
        targets = re.findall(r'"%dp0%\\([^"]+?\.(?:exe|js))"', text, re.IGNORECASE)
        scripts = [t for t in targets if t.lower().endswith(".js")]
        binaries = [t for t in targets if t.lower().endswith(".exe") and t.lower() != "node.exe"]
        if scripts:
            node = Path(found).parent / "node.exe"
            return [str(node) if node.exists() else "node", str(Path(found).parent / scripts[0])]
        if binaries:
            return [str(Path(found).parent / binaries[0])]
    return [found]


def harness_version(name):
    out = subprocess.run(resolve_executable(name) + ["--version"],
                         capture_output=True, text=True, encoding="utf-8")
    match = re.search(r"\d+\.\d+\.\d+", out.stdout or "")
    return match.group(0) if match else ""


def check_version(name, version):
    if version != PINNED_VERSIONS[name]:
        sys.exit(f"error: {name} is {version or 'unknown'}, pinned {PINNED_VERSIONS[name]}")


# ---------------------------------------------------------------- processes

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class _BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount",
                    "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


class ProcessTree:
    def __init__(self, proc):
        self.proc, self.job = proc, None
        if os.name == "nt":
            self.job = _kernel32.CreateJobObjectW(None, None)
            limits = _ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = 0x2000
            _kernel32.SetInformationJobObject(self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
            if not _kernel32.AssignProcessToJobObject(self.job, int(proc._handle)):
                proc.kill()
                raise OSError(ctypes.get_last_error(), "could not assign the harness to a job object")

    def kill(self):
        if os.name == "nt":
            _kernel32.TerminateJobObject(self.job, 1)
            _kernel32.CloseHandle(self.job)
        else:
            import signal
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.proc.wait()


def run_process(argv, cwd, stdin_text, out_path, err_path, timeout, env=None):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    full_env = {**os.environ, **(env or {})}
    with open(out_path, "w", encoding="utf-8", newline="\n") as out, \
         open(err_path, "w", encoding="utf-8") as err:
        proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE, stdout=out, stderr=err, text=True,
                                encoding="utf-8", env=full_env, start_new_session=(os.name != "nt"))
        tree = ProcessTree(proc)
        try:
            proc.communicate(stdin_text, timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            tree.kill()
        return (None if timed_out else proc.returncode), timed_out


def port_open(port=FIXTURE_PORT):
    for host in ("127.0.0.1", "::1"):
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            continue
    return False


def require_free_port(when):
    if port_open():
        sys.exit(f"error: port {FIXTURE_PORT} is in use {when}; stop that process and rerun")


# ---------------------------------------------------------------- workspaces

def fixture_digest():
    h = hashlib.sha256()
    for path in sorted(p for p in FIXTURE.rglob("*") if p.is_file()):
        h.update(path.relative_to(FIXTURE).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return h.hexdigest()


def git(args, cwd, capture=False):
    out = subprocess.run(["git"] + GIT_ID + args, cwd=cwd, check=True,
                         capture_output=True, text=True, encoding="utf-8")
    return out.stdout if capture else None


def make_workspace(parent, with_fixture):
    repo = Path(parent) / "repo"
    if with_fixture:
        shutil.copytree(FIXTURE, repo)
    else:
        repo.mkdir()
    git(["init", "-q"], repo)
    (repo / ".git" / "info" / "exclude").write_text(
        ".claude/\n.codex/\n.opencode/\n.pi/\n.agents/\n", encoding="utf-8")
    if with_fixture:
        git(["add", "-A"], repo)
        git(["commit", "-q", "-m", "init"], repo)
    else:
        git(["commit", "-q", "--allow-empty", "-m", "init"], repo)
    return repo


def capture_diff(repo):
    git(["add", "-A"], repo)
    return git(["diff", "--cached", "--ignore-cr-at-eol"], repo, capture=True)


def added_lines_chars(diff_text):
    lines = chars = 0
    in_hunk = False
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            in_hunk = False
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk and line.startswith("+"):
            lines += 1
            chars += len(line[1:].rstrip("\r"))
    return lines, chars


class Timer:
    def __enter__(self):
        self.start = time.monotonic()
        return self

    def __exit__(self, *exc):
        self.seconds = round(time.monotonic() - self.start, 1)


def temp_dir():
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


# ---------------------------------------------------------------- prices and cost

def load_prices():
    return json.loads((ROOT / "prices.json").read_text(encoding="utf-8"))


def price_for(prices, key):
    provider, model = key
    for entry in prices["prices"]:
        if entry["provider"] == provider and entry["model"] == model:
            return entry
    raise KeyError(f"no price for {provider}/{model} in prices.json")


def context_of(call):
    return sum(int(call[k] or 0) for k in ("input_uncached", "cache_write", "cache_write_1h", "cache_read"))


def input_cost(call, p):
    w1h = int(call["cache_write_1h"] or 0)
    if w1h and p["cache_write_1h"] is None:
        raise ValueError(f"1-hour cache writes reported but {p['provider']}/{p['model']} has no 1-hour price")
    return (p["input"] * int(call["input_uncached"] or 0)
            + p["cache_write"] * int(call["cache_write"] or 0)
            + (p["cache_write_1h"] or 0) * w1h
            + p["cache_read"] * int(call["cache_read"] or 0)) / 1e6


def output_cost(tokens, p):
    return p["output"] * int(tokens or 0) / 1e6


# ---------------------------------------------------------------- files

def read_jsonl(path):
    events = []
    if not Path(path).exists():
        return events
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def spread(values):
    values = [float(v) for v in values if v not in (None, "")]
    if not values:
        return {"n": 0}
    return {"n": len(values), "min": min(values), "median": statistics.median(values),
            "mean": round(statistics.fmean(values), 4), "max": max(values)}


def write_csv(path, header, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def append_csv(path, header, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)


def read_csv(path):
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def rel(path):
    path = Path(path).resolve()
    return path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else path.as_posix()


def now_utc():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
