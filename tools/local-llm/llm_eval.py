#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest>=8"]
# ///
"""Quality eval for local models: small, automatically graded coding tasks.

Each model is served with llama-server (same settings we intend to deploy) and
given tasks shaped like our real workloads: MCP-style delegation over the OpenAI
chat API, and an agentic bug fix over the Anthropic Messages API (what Claude
Code speaks). Grading is mechanical: keyword facts, exact JSON, or pytest runs.

Model-written code is executed with pytest in a throwaway directory with a
timeout. Only run models you trust to the same degree as any code you download.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

log = logging.getLogger("llm-eval")

HERE = Path(__file__).resolve().parent
LLAMA_CPP_DIR = Path(
    os.environ.get("LLAMA_CPP_DIR", Path.home() / ".cache/legiontools7a/llama.cpp")
)
MODELS_DIR = Path(os.environ.get("LLM_MODELS_DIR", "/srv/llm/models"))
BACKEND_ENV = {"rocm": {"ROCBLAS_USE_HIPBLASLT": "1"}, "vulkan": {}}
PYTEST_TIMEOUT = 60


# --------------------------------------------------------------------------- client


class Client:
    """Minimal HTTP client for llama-server's OpenAI and Anthropic endpoints."""

    def __init__(self, base_url: str, timeout: float = 900) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError(f"unsupported URL scheme: {base_url}")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(  # noqa: S310 - scheme checked in __init__
            self.base_url + path,
            data=json.dumps(payload).encode(),
            headers={"content-type": "application/json", "x-api-key": "local"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 - http(s) only
            return json.load(resp)

    def chat(self, prompt: str, *, system: str = "", max_tokens: int = 8192) -> str:
        messages = [{"role": "system", "content": system}] if system else []
        messages.append({"role": "user", "content": prompt})
        payload = {"messages": messages, "max_tokens": max_tokens, "temperature": 0.2}
        data = self._post("/v1/chat/completions", payload)
        return data["choices"][0]["message"].get("content") or ""

    def messages(self, payload: dict) -> dict:
        return self._post("/v1/messages", payload)


# --------------------------------------------------------------------------- helpers


def extract_code(text: str) -> str:
    """Return the first fenced code block (python preferred), else the text."""
    blocks = re.findall(r"```(\w*)\n(.*?)```", text, re.S)
    for lang, body in blocks:
        if lang.lower() in ("python", "py"):
            return body
    return blocks[0][1] if blocks else text


def extract_json(text: str) -> dict | None:
    candidate = extract_code(text) if "```" in text else text
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(candidate[start : end + 1])
    except json.JSONDecodeError:
        return None


def run_pytest_output(workdir: Path) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(workdir)],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=PYTEST_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "pytest timed out"
    return proc.returncode == 0, proc.stdout + proc.stderr


def run_pytest(workdir: Path) -> bool:
    return run_pytest_output(workdir)[0]


def first_failure(output: str) -> str:
    """The first assertion/error line from pytest output, for result details."""
    for line in output.splitlines():
        if line.startswith(("E ", "FAILED", "ERROR")):
            return line.strip()[:160]
    return output.strip().splitlines()[-1][:160] if output.strip() else ""


@dataclass
class Outcome:
    passed: bool
    detail: str = ""


@dataclass
class Task:
    name: str
    run: Callable[[Client], Outcome]
    kind: str = "mcp"


# --------------------------------------------------------------------------- tasks


def task_log_root_cause(client: Client) -> Outcome:
    lines = []
    for i in range(1200):  # ~20K tokens: fits the 32K eval context
        lines.append(f"2026-10-04T10:{i // 60 % 60:02d}:{i % 60:02d} INFO api request ok id={i}")
        if i == 712:
            lines.append(
                "2026-10-04T10:28:32 WARN db pool: all 20 connections in use "
                "(max_connections=20), waiting"
            )
        if 713 <= i <= 760:
            lines.append(f"2026-10-04T10:28:{i % 60:02d} ERROR request id={i} timed out after 30s")
    answer = client.chat(
        "Here is a service log. In one sentence, what is the root cause of the errors?\n\n"
        + "\n".join(lines)
    ).lower()
    ok = "pool" in answer and ("max_connections" in answer or "20" in answer)
    return Outcome(ok, answer[:200])


DIFF = """--- a/paging.py
+++ b/paging.py
@@ def paginate(items, page, size):
-    start = (page - 1) * size
-    return items[start:start + size]
+    start = (page - 1) * size
+    return items[start:start + size - 1]
"""


def task_diff_bug(client: Client) -> Outcome:
    answer = client.chat(
        "Review this diff. Is there a bug? Name the exact problem in one or two sentences.\n\n"
        + DIFF
    ).lower()
    ok = any(k in answer for k in ("off-by-one", "off by one", "size - 1", "size-1", "one fewer"))
    return Outcome(ok, answer[:200])


DURATION_IMPL = '''import re

def parse_duration(text):
    """Parse '1h30m', '45s', '2h', '10m5s' into seconds. Raise ValueError if invalid."""
    m = re.fullmatch(r"(?:(\\d+)h)?(?:(\\d+)m)?(?:(\\d+)s)?", text)
    if not text or not m:
        raise ValueError(text)
    h, mi, s = (int(g) if g else 0 for g in m.groups())
    return h * 3600 + mi * 60 + s
'''
DURATION_MUTANTS = [
    DURATION_IMPL.replace("mi * 60", "mi * 6"),
    DURATION_IMPL.replace("if not text or not m:\n        raise ValueError(text)\n", ""),
]


def task_write_tests(client: Client) -> Outcome:
    answer = client.chat(
        "Write a pytest test module for this function, which lives in `duration.py`. "
        "Import it with `from duration import parse_duration`. Cover normal cases and "
        "invalid input. Reply with only the Python code.\n\n" + DURATION_IMPL
    )
    tests = extract_code(answer)
    results, why = [], ""
    for impl in [DURATION_IMPL, *DURATION_MUTANTS]:
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "duration.py").write_text(impl)
            (Path(d) / "test_duration.py").write_text(tests)
            passed, output = run_pytest_output(Path(d))
            if impl is DURATION_IMPL and not passed:
                why = " | " + first_failure(output)
            results.append(passed)
    ok = results[0] and not any(results[1:])
    caught = [not r for r in results[1:]]
    return Outcome(ok, f"correct={results[0]} mutants_caught={caught}{why}")


IMPLEMENT_SPEC = """Implement these two functions in one Python module. Reply with only the code.

1. merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]
   Merge overlapping or touching intervals (e.g. (1,3) and (3,5) merge). Input may be
   unsorted. Return sorted, merged intervals. Empty input returns [].

2. slugify(text: str) -> str
   Lowercase; replace each run of characters that are not a-z or 0-9 with a single
   hyphen; strip leading/trailing hyphens. "Hello, World!" -> "hello-world".
"""
IMPLEMENT_TESTS = """from solution import merge_intervals, slugify

def test_merge():
    assert merge_intervals([]) == []
    assert merge_intervals([(5, 6), (1, 3), (2, 4)]) == [(1, 4), (5, 6)]
    assert merge_intervals([(1, 3), (3, 5)]) == [(1, 5)]
    assert merge_intervals([(1, 10), (2, 3)]) == [(1, 10)]

def test_slugify():
    assert slugify("Hello, World!") == "hello-world"
    assert slugify("  --Already-Slugged--  ") == "already-slugged"
    assert slugify("Cafe 2026 / Menu") == "cafe-2026-menu"
"""


def task_implement(client: Client) -> Outcome:
    code = extract_code(client.chat(IMPLEMENT_SPEC))
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "solution.py").write_text(code)
        (Path(d) / "test_solution.py").write_text(IMPLEMENT_TESTS)
        ok = run_pytest(Path(d))
    return Outcome(ok)


EMAIL = """Hi team,
Invoice INV-2291 from Northwind Traders for $4,812.50 is due on 14 November 2026.
Please route approval to Dana Whitfield in finance. Thanks, Sam"""
EMAIL_EXPECTED = {
    "invoice_id": "INV-2291",
    "vendor": "Northwind Traders",
    "amount": 4812.5,
    "due_date": "2026-11-14",
}


def task_json_extract(client: Client) -> Outcome:
    answer = client.chat(
        "Extract invoice_id, vendor, amount (number), due_date (YYYY-MM-DD) from this email. "
        "Reply with only a JSON object.\n\n" + EMAIL
    )
    data = extract_json(answer)
    ok = data is not None and all(data.get(k) == v for k, v in EMAIL_EXPECTED.items())
    return Outcome(ok, json.dumps(data)[:200] if data else answer[:200])


# --- agentic task: tool use over the Anthropic Messages API -----------------

AGENT_REPO = {
    "stats.py": """def mean(values):
    return sum(values) / len(values)


def median(values):
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return ordered[mid]
""",
    "test_stats.py": """from stats import mean, median

def test_mean():
    assert mean([1, 2, 3, 4]) == 2.5

def test_median_odd():
    assert median([3, 1, 2]) == 2

def test_median_even():
    assert median([4, 1, 3, 2]) == 2.5
""",
}
AGENT_TOOLS = [
    {
        "name": "list_files",
        "description": "List files in the repository.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "read_file",
        "description": "Read a file from the repository.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Overwrite a file in the repository with new content.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
    {
        "name": "run_tests",
        "description": "Run the test suite with pytest and return the output.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


@dataclass
class Sandbox:
    root: Path
    calls: list[str] = field(default_factory=list)

    def _resolve(self, rel: str) -> Path:
        path = (self.root / rel).resolve()
        if self.root.resolve() not in path.parents:
            raise ValueError(f"path outside repository: {rel}")
        return path

    def execute(self, name: str, args: dict) -> str:
        self.calls.append(name)
        try:
            if name == "list_files":
                return "\n".join(sorted(p.name for p in self.root.iterdir() if p.is_file()))
            if name == "read_file":
                return self._resolve(args["path"]).read_text()
            if name == "write_file":
                self._resolve(args["path"]).write_text(args["content"])
                return "ok"
            if name == "run_tests":
                proc = subprocess.run(
                    [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                    cwd=self.root,
                    capture_output=True,
                    text=True,
                    timeout=PYTEST_TIMEOUT,
                    check=False,
                )
                return (proc.stdout + proc.stderr)[-3000:]
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
            return f"error: {exc}"
        return f"error: unknown tool {name}"


def task_agentic_fix(client: Client, max_turns: int = 12) -> Outcome:
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for name, content in AGENT_REPO.items():
            (root / name).write_text(content)
        box = Sandbox(root)
        messages = [
            {
                "role": "user",
                "content": "The test suite in this repository fails. Find and fix the bug "
                "in the source code (do not modify the tests). Use the tools, run the "
                "tests to confirm, then reply with a one-line summary.",
            }
        ]
        for _ in range(max_turns):
            resp = client.messages(
                {
                    "model": "local",
                    "max_tokens": 4096,
                    "system": "You are a careful software engineer working in a small repo.",
                    "tools": AGENT_TOOLS,
                    "messages": messages,
                }
            )
            messages.append({"role": "assistant", "content": resp["content"]})
            uses = [b for b in resp["content"] if b.get("type") == "tool_use"]
            if not uses:
                break
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": u["id"],
                            "content": box.execute(u["name"], u.get("input") or {}),
                        }
                        for u in uses
                    ],
                }
            )
        tests_intact = (root / "test_stats.py").read_text() == AGENT_REPO["test_stats.py"]
        ok = tests_intact and run_pytest(root)
        return Outcome(ok, f"tools={box.calls} tests_intact={tests_intact}")


TASKS = [
    Task("log_root_cause", task_log_root_cause),
    Task("diff_bug", task_diff_bug),
    Task("write_tests", task_write_tests),
    Task("implement", task_implement),
    Task("json_extract", task_json_extract),
    Task("agentic_fix", task_agentic_fix, kind="agentic"),
]


# --------------------------------------------------------------------------- server


def server_command(binary: Path, model: Path, port: int, ctx: int) -> list[str]:
    return [
        str(binary),
        "-m", str(model),
        "-ngl", "99",
        "-fa", "on",
        "-ctk", "q8_0",
        "-ctv", "q8_0",
        "-c", str(ctx),
        "-np", "1",
        "--jinja",
        "--host", "127.0.0.1",
        "--port", str(port),
    ]  # fmt: skip


def wait_healthy(url: str, proc: subprocess.Popen, timeout: float = 600) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"llama-server exited with {proc.returncode}")
        try:
            with urllib.request.urlopen(url + "/health", timeout=5) as resp:  # noqa: S310 - local URL
                if resp.status == 200:
                    return
        except OSError:
            pass
        time.sleep(2)
    raise RuntimeError("llama-server did not become healthy")


def evaluate(client: Client, tasks: list[Task], repeats: int) -> list[dict]:
    rows = []
    for task in tasks:
        for attempt in range(repeats):
            start = time.monotonic()
            try:
                outcome = task.run(client)
            except Exception as exc:  # a crashing task is a failed task
                outcome = Outcome(False, f"exception: {exc}"[:200])
            elapsed = time.monotonic() - start
            log.info(
                "  %-15s #%d %s %.1fs",
                task.name,
                attempt + 1,
                "PASS" if outcome.passed else "fail",
                elapsed,
            )
            rows.append(
                {
                    "task": task.name,
                    "kind": task.kind,
                    "attempt": attempt + 1,
                    "passed": outcome.passed,
                    "seconds": round(elapsed, 1),
                    "detail": outcome.detail,
                }
            )
    return rows


def summarize(rows: list[dict]) -> str:
    models = list(dict.fromkeys(r["model"] for r in rows))
    tasks = list(dict.fromkeys(r["task"] for r in rows))
    header = ["Model", *tasks, "Total", "Time (s)"]
    lines = ["| " + " | ".join(header) + " |", "|" + " --- |" * len(header)]
    for model in models:
        mine = [r for r in rows if r["model"] == model]
        cells = []
        for task in tasks:
            tr = [r for r in mine if r["task"] == task]
            cells.append(f"{sum(r['passed'] for r in tr)}/{len(tr)}")
        total = f"{sum(r['passed'] for r in mine)}/{len(mine)}"
        seconds = f"{sum(r['seconds'] for r in mine):.0f}"
        lines.append("| " + " | ".join([model, *cells, total, seconds]) + " |")
    return "\n".join(lines) + "\n"


def run_models(args: argparse.Namespace) -> int:
    paths = [Path(p) if Path(p).is_absolute() else args.models_dir / p for p in args.models]
    missing = [p for p in paths if not p.exists()]
    if missing:
        log.error("model file not found: %s", ", ".join(map(str, missing)))
        return 1
    tasks = [t for t in TASKS if not args.tasks or t.name in args.tasks]
    args.out_dir.mkdir(parents=True, exist_ok=True)
    out = args.out_dir / f"eval-{datetime.now():%Y%m%d-%H%M%S}.jsonl"
    binary = args.llama_cpp_dir / f"build-{args.backend}" / "bin" / "llama-server"
    rows = []
    for path in paths:
        name = path.name.removesuffix(".gguf")
        log.info("%s (%s)", name, args.backend)
        cmd = server_command(binary, path, args.port, args.ctx)
        env = {**os.environ, **BACKEND_ENV[args.backend]}
        with (args.out_dir / f"{name}.server.log").open("w") as server_log:
            proc = subprocess.Popen(cmd, env=env, stdout=server_log, stderr=subprocess.STDOUT)
            try:
                url = f"http://127.0.0.1:{args.port}"
                wait_healthy(url, proc)
                results = evaluate(Client(url), tasks, args.repeats)
            except RuntimeError as exc:
                log.error("%s: %s", name, exc)
                results = [{"task": "server", "kind": "-", "attempt": 1, "passed": False,
                            "seconds": 0, "detail": str(exc)}]  # fmt: skip
            finally:
                proc.terminate()
                proc.wait(timeout=60)
        for r in results:
            r.update(model=name, backend=args.backend)
            rows.append(r)
            with out.open("a") as f:
                f.write(json.dumps(r) + "\n")
    print(summarize(rows))
    log.info("raw results: %s", out)
    return 0


def task_list(value: str) -> list[str]:
    names = [n.strip() for n in value.split(",") if n.strip()]
    unknown = sorted(set(names) - {t.name for t in TASKS})
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown task(s): {', '.join(unknown)}")
    return names


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="llm-eval", description=__doc__.splitlines()[0])
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="serve each model and run the tasks")
    r.add_argument("models", nargs="+", help="GGUF paths (absolute or relative to models dir)")
    r.add_argument("--backend", choices=BACKEND_ENV, default="vulkan")
    r.add_argument(
        "--tasks",
        type=task_list,
        help=f"comma-separated subset of: {','.join(t.name for t in TASKS)}",
    )
    r.add_argument("--repeats", type=int, default=3, help="attempts per task (default: 3)")
    r.add_argument("--ctx", type=int, default=32768, help="server context (default: 32768)")
    r.add_argument("--port", type=int, default=8091)
    r.add_argument("--models-dir", type=Path, default=MODELS_DIR, help=argparse.SUPPRESS)
    r.add_argument("--llama-cpp-dir", type=Path, default=LLAMA_CPP_DIR, help=argparse.SUPPRESS)
    r.add_argument("--out-dir", type=Path, default=HERE / "results")
    rep = sub.add_parser("report", help="re-print the summary of a results file")
    rep.add_argument("results", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s: %(message)s"
    )
    if args.command == "report":
        rows = [json.loads(x) for x in args.results.read_text().splitlines() if x]
        print(summarize(rows))
        return 0
    return run_models(args)


if __name__ == "__main__":
    sys.exit(main())
