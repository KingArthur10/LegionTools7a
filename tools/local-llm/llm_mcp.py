#!/usr/bin/env python3
"""MCP server that hands work in Claude Code to the local model.

Claude passes file paths, this server reads the files itself and asks the local
model (via llama-swap), and only a short result goes back into Claude's context.

Tools:
  local_ask     answer a question about files, directories or globs
  local_search  find code by meaning, using the embedding model
  local_task    have a local Claude Code worker (claude-local) implement a
                well-specified change, then run the tests and report the diff

Only files under the project directory (the server's working directory, or
--root) and the system temp directory (or --allow DIRs) are read. Names that look
like secrets (.env, keys, credentials) are always skipped.

  llm_mcp.py                       # serve MCP over stdio (what Claude Code runs)
  llm_mcp.py ask QUESTION PATH...  # same as the tool, from a shell
  llm_mcp.py search QUERY [PATH]
  llm_mcp.py task TASK [--file F]... [--test CMD]
"""

from __future__ import annotations

import argparse
import array
import base64
import fcntl
import fnmatch
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

log = logging.getLogger("llm-mcp")

HERE = Path(__file__).resolve().parent

DEFAULT_URL = "http://127.0.0.1:8080"
CACHE_DIR = (
    Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "legiontools7a" / "llm-mcp"
)
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SERVER_VERSION = "0.2.0"

MAX_FILE_BYTES = 2_000_000
MAX_INPUT_TOKENS = 100_000  # ~4-5 min at the measured ~370 tokens/s; beyond that, narrow paths
ANSWER_TOKENS = 1500
NOTE_TOKENS = 1024
PROMPT_OVERHEAD_TOKENS = 1024
# Chunks are cut by characters using each file's measured characters per token
# (code ~3.5, logs full of digits ~1.6); leave room for uneven spread within a file.
TOKEN_SAFETY = 0.85
NOTHING = "NOTHING RELEVANT"

SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".tox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", ".cache",
    ".ssh", ".gnupg",
}  # fmt: skip
SECRET_NAMES = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.kdbx", "id_rsa*",
    "id_ecdsa*", "id_ed25519*", ".netrc", ".pgpass", ".git-credentials",
    "*credentials*", "*secret*",
)  # fmt: skip

SEARCH_MAX_FILE_BYTES = 300_000  # bigger files are usually vendored or generated
SEARCH_MAX_NEW_CHUNKS = 1200  # per call: ~1 min at ~20 chunks/s; the rest on later calls
SEARCH_WINDOW = 40  # lines per embedded chunk
SEARCH_STEP = 30  # 10 lines of overlap
SEARCH_CHUNK_CHARS = 4000
EMBED_BATCH = 8
# Generated files that match many queries and are never the answer.
SEARCH_SKIP_NAMES = ("*.lock", "package-lock.json", "*.min.js", "*.min.css", "*.map", "*.svg")
SEARCH_QUERY_PREFIX = (
    "Instruct: Given a question about a codebase, retrieve the code or text that answers it\n"
    "Query: "
)

TASK_TIMEOUT = 15 * 60
TASK_MAX_TIMEOUT = 28 * 60  # Claude Code drops a silent stdio tool call after 30 min
# Workers edit the working tree, so tasks in one repository queue behind each other.
TASK_QUEUE_WAIT = 30 * 60
SEARCH_QUEUE_WAIT = 10 * 60
QUEUE_PROGRESS_SECONDS = 15
# Tool calls run on threads; how many llama-server serves at once is up to its slots.
MAX_CONCURRENT_CALLS = 8
TEST_TIMEOUT = 5 * 60
TASK_SUMMARY_CHARS = 1500
TEST_OUTPUT_LINES = 30
# No bare Edit/Write: a rule without a path allows writing anywhere. acceptEdits mode
# already approves edits inside the project, and -p mode refuses everything else.
WORKER_TOOLS = ("Read", "Grep", "Glob")
# Existing files matching these are protected unless listed as targets: a worker that
# can't make tests pass tends to rewrite them.
TEST_FILE_PATTERNS = ("test_*", "*_test.*", "*.test.*", "*.spec.*", "conftest.py")
# Claude Code session variables: a nested session must not attach to the parent's.
STRIPPED_ENV_PREFIXES = ("CLAUDECODE", "CLAUDE_CODE_", "CLAUDE_PID", "CLAUDE_EFFORT")

WORKER_PROMPT = """You are implementing one well-specified change in this repository. Another
engineer will review your diff.

Task:
{task}
{files}{tests}
Rules:
- Change only what the task needs, following the existing code style.
- Do not modify existing tests unless the task says so.
- Do not run git commands or touch files outside this repository.
- When done, reply with a short summary: files changed, and anything unfinished or uncertain."""

SYSTEM_ASK = """You answer questions about files for another AI assistant that cannot see them.
Files appear under `=== path (lines a-b of n) ===` headers, each line as `N: text`.
- Answer only from the files. If the answer is not there, say so plainly.
- Cite evidence as path:line or path:start-end.
- Be concise: lead with the answer, then the key evidence. No preamble.
- Quote code only when the exact text matters, and keep quotes short."""

SYSTEM_MAP = f"""You are reading one part of a larger set of files for another AI assistant.
Files appear under `=== path (lines a-b of n) ===` headers, each line as `N: text`.
Write brief notes with only what helps answer the question, each with path:line citations.
If nothing in this part is relevant, reply exactly: {NOTHING}"""

SYSTEM_REDUCE = """Below are notes taken while reading parts of a set of files, with path:line
citations. Combine them into one answer to the question for another AI assistant.
Keep the citations. If notes conflict, say so. If none answer the question, say the files
don't contain the answer. Be concise: lead with the answer. No preamble."""

INSTRUCTIONS = """Local model on this machine. Pass file paths, directories or globs: the
server reads the files itself, so their contents never enter your context.
- local_task: delegate writing code. Give a precise spec (behaviour, file names, interfaces,
  edge cases) and a test command; a local Claude Code worker implements it and the server
  runs the tests. Use it for well-specified, self-contained pieces (boilerplate, UI markup,
  CRUD handlers, tests from a spec), not for security-sensitive or subtle code. Always review
  the reported diff (git diff) before building on it.
- local_ask: questions over large or many files: logs, unfamiliar modules, "where/how is X
  done", summaries of a directory. Don't paste content into it; write output to a file and
  pass the path.
- local_search: find where something is implemented by meaning when you don't know the
  names to grep for.
Answers come from a smaller model: treat them as leads and read the cited lines yourself
before editing code or stating facts that matter. Read files you are about to edit
directly, and use Read for a quick look at one small file: that is cheaper and exact."""

TOOLS = [
    {
        "name": "local_task",
        "description": (
            "Delegate a well-specified code change to a local Claude Code worker running on "
            "the local model (git repositories only). The worker edits files in the project "
            "and may run only test_command. Afterwards the server runs test_command itself "
            "and returns the test result, the changed files with line counts, and the "
            "worker's summary. Saves your output tokens on code you would otherwise write. "
            "Review the diff before relying on it. Takes minutes; one task at a time."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Precise spec: behaviour, files, interfaces, edge cases.",
                },
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Files to create or change, relative to the project.",
                },
                "protect": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Files the worker must not change. Existing test files "
                    "(test_*, *_test.*, *.spec.*, tests/) are protected automatically unless "
                    "listed in files.",
                },
                "test_command": {
                    "type": "string",
                    "description": "Command that verifies the work, e.g. 'uv run pytest tests/'.",
                },
                "timeout_minutes": {
                    "type": "integer",
                    "description": f"Worker time limit (default {TASK_TIMEOUT // 60}, "
                    f"max {TASK_MAX_TIMEOUT // 60}).",
                },
            },
            "required": ["task"],
        },
    },
    {
        "name": "local_ask",
        "description": (
            "Ask the local model a question about files. It reads the paths itself "
            "(files, directories or globs such as 'src/**/*.py'), splits input larger than "
            "its context, and returns a short answer with path:line citations. Use for big "
            "logs, many files or summaries, to keep their contents out of your context."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "What you want to know."},
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Files, directories or globs, relative to the project.",
                },
            },
            "required": ["question", "paths"],
        },
    },
    {
        "name": "local_search",
        "description": (
            "Semantic search over the project's text files using a local embedding model. "
            "Returns the best-matching path:start-end ranges with a one-line preview. Use "
            "when you don't know the identifiers to grep for. The index is cached and only "
            "changed files are re-embedded."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What you are looking for."},
                "path": {
                    "type": "string",
                    "description": "Directory to search (default: the whole project).",
                },
                "limit": {"type": "integer", "description": "Results to return (default 8)."},
            },
            "required": ["query"],
        },
    },
]


class ToolError(Exception):
    """A problem to report back to the caller as a tool error."""


class Cancelled(ToolError):
    """The caller cancelled the request (MCP notifications/cancelled)."""


Progress = Callable[[int, int, str], None]


def no_progress(done: int, total: int, message: str) -> None:
    pass


def check(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled("cancelled")


def root_key(root: Path) -> str:
    return hashlib.sha256(str(root).encode()).hexdigest()[:16]


@contextmanager
def queue_lock(
    path: Path,
    waiting: str,
    max_wait: float,
    progress: Progress = no_progress,
    cancel: threading.Event | None = None,
) -> Iterator[None]:
    """Hold an exclusive lock on `path`, waiting (with progress) while another holds it.

    flock is per open file, so this queues threads of one server as well as servers
    of other Claude Code sessions.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as lock:
        start = time.monotonic()
        last = start - QUEUE_PROGRESS_SECONDS
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                check(cancel)
                now = time.monotonic()
                if now - start >= max_wait:
                    raise ToolError(f"{waiting}: gave up after {max_wait / 60:.0f} min") from None
                if now - last >= QUEUE_PROGRESS_SECONDS:
                    progress(int(now - start), int(max_wait), f"{waiting} ({now - start:.0f} s)")
                    last = now
                if cancel is not None:
                    cancel.wait(0.5)
                else:
                    time.sleep(0.5)
        yield


# --------------------------------------------------------------------------- client


class Client:
    """Minimal client for llama-swap's OpenAI-compatible endpoints."""

    def __init__(self, base_url: str, model: str, embed_model: str, timeout: float = 900) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError(f"unsupported URL scheme: {base_url}")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.embed_model = embed_model
        self.timeout = timeout
        self._props: dict | None = None

    def _request(self, path: str, payload: dict | None = None) -> dict:
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(  # noqa: S310 - scheme checked in __init__
            self.base_url + path, data=data, headers={"content-type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                return json.load(resp)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:300]
            raise ToolError(f"local model server returned HTTP {e.code}: {body}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            raise ToolError(
                f"cannot reach the local model server at {self.base_url} ({e}). "
                "Is llama-swap running? (systemctl status llama-swap)"
            ) from e

    def props(self) -> dict:
        """The chat model's server settings (loads the model if needed)."""
        if self._props is None:
            self._props = self._request(f"/upstream/{self.model}/props")
        return self._props

    def context_size(self) -> int:
        """Context of one request slot."""
        return int(self.props()["default_generation_settings"]["n_ctx"])

    def slots(self) -> int:
        """Requests the chat model serves at once (llama-server -np)."""
        return max(1, int(self.props().get("total_slots") or 1))

    def count_tokens(self, text: str) -> int:
        """Exact token count from the chat model's tokenizer (fast; no generation)."""
        if not text:
            return 0
        return len(self._request(f"/upstream/{self.model}/tokenize", {"content": text})["tokens"])

    def chat(self, system: str, prompt: str, max_tokens: int) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.2,
        }
        text = self._request("/v1/chat/completions", payload)["choices"][0]["message"]
        return re.sub(r"<think>.*?</think>", "", text.get("content") or "", flags=re.S).strip()

    def embed(self, texts: list[str]) -> list[list[float]]:
        data = self._request("/v1/embeddings", {"model": self.embed_model, "input": texts})
        rows = sorted(data["data"], key=lambda r: r["index"])
        return [r["embedding"] for r in rows]


# --------------------------------------------------------------------------- files


def is_secret(path: Path) -> bool:
    if any(part in (".ssh", ".gnupg") for part in path.parts):
        return True
    name = path.name.lower()
    return any(fnmatch.fnmatch(name, pattern) for pattern in SECRET_NAMES)


def is_text(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return b"\0" not in f.read(8192)
    except OSError:
        return False


class Workspace:
    """Resolves the paths Claude passes, keeping reads inside the allowed directories."""

    def __init__(self, root: Path, extra: list[Path] | None = None) -> None:
        self.root = root.resolve()
        self.allowed = [self.root, *(p.resolve() for p in extra or [])]

    def display(self, path: Path) -> str:
        return str(path.relative_to(self.root)) if path.is_relative_to(self.root) else str(path)

    def resolve(self, spec: str) -> Path:
        path = Path(spec).expanduser()
        path = (path if path.is_absolute() else self.root / path).resolve()
        if not any(path.is_relative_to(d) for d in self.allowed):
            raise ToolError(
                f"{spec}: outside the allowed directories ({', '.join(map(str, self.allowed))})"
            )
        return path

    def _walk(self, directory: Path) -> Iterator[Path]:
        try:
            out = subprocess.run(
                ["git", "-C", str(directory), "ls-files", "-z", "-co", "--exclude-standard"],
                capture_output=True,
                check=True,
                timeout=30,
            ).stdout
            for name in sorted(filter(None, out.decode(errors="replace").split("\0"))):
                path = directory / name
                if not SKIP_DIRS.intersection(Path(name).parts[:-1]):
                    yield path
            return
        except (subprocess.SubprocessError, OSError):
            pass  # not a git work tree: plain walk
        for dirpath, dirnames, filenames in os.walk(directory):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for name in sorted(filenames):
                yield Path(dirpath) / name

    def expand(self, specs: list[str]) -> tuple[list[Path], list[str]]:
        """Files for the given paths, dirs and globs, plus notes on what was skipped."""
        found: list[Path] = []
        skipped: list[str] = []
        for spec in specs:
            if path_is_glob(spec):
                if Path(spec).is_absolute():
                    raise ToolError(f"{spec}: globs must be relative to the project")
                candidates = [p for p in sorted(self.root.glob(spec)) if p.is_file()]
                if not candidates:
                    skipped.append(f"{spec}: no matches")
            else:
                path = self.resolve(spec)
                if path.is_dir():
                    candidates = list(self._walk(path))
                elif path.is_file():
                    candidates = [path]
                else:
                    skipped.append(f"{spec}: not found")
                    continue
            explicit = len(candidates) == 1 and not path_is_glob(spec)
            for path in candidates:
                path = self.resolve(str(path))
                name = self.display(path)
                if path in found or not path.is_file():
                    continue
                if is_secret(path):
                    skipped.append(f"{name}: looks like a secret, not read")
                elif path.stat().st_size > MAX_FILE_BYTES:
                    skipped.append(f"{name}: larger than {MAX_FILE_BYTES // 1_000_000} MB")
                elif not is_text(path):
                    if explicit:
                        skipped.append(f"{name}: binary")
                else:
                    found.append(path)
        return found, skipped


def path_is_glob(spec: str) -> bool:
    return any(c in spec for c in "*?[")


def read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


# --------------------------------------------------------------------------- local_ask


def segments(name: str, lines: list[str], budget: int) -> Iterator[str]:
    """A file as numbered-line blocks of at most `budget` characters, each with a header."""

    def header(last: int) -> str:
        return f"=== {name} (lines {first}-{last} of {len(lines)}) ===\n"

    header_room = len(name) + 60
    room = max(budget - header_room, 200)
    block: list[str] = []
    size = 0
    first = 1
    for number, line in enumerate(lines, 1):
        text = f"{number}: {line}"[:room]
        if block and size + len(text) + 1 > room:
            yield header(number - 1) + "\n".join(block)
            block, size, first = [], 0, number
        block.append(text)
        size += len(text) + 1
    if block:
        yield header(len(lines)) + "\n".join(block)
    else:
        yield f"=== {name} (empty file) ==="


@dataclass
class Block:
    text: str
    tokens: int


def file_blocks(client: Client, name: str, lines: list[str], budget: int) -> list[Block]:
    """A file cut into blocks of at most `budget` tokens, using its measured token density."""
    text = "\n".join(f"{n}: {line}" for n, line in enumerate(lines, 1))
    tokens = client.count_tokens(text) or 1
    chars_per_token = max(len(text), 1) / tokens
    char_budget = int(budget * chars_per_token * TOKEN_SAFETY)
    return [Block(b, int(len(b) / chars_per_token) + 1) for b in segments(name, lines, char_budget)]


def pack(blocks: list[Block], budget: int) -> list[str]:
    """Group blocks into chunks of at most `budget` tokens."""
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for block in blocks:
        if current and size + block.tokens > budget:
            chunks.append("\n\n".join(current))
            current, size = [], 0
        current.append(block.text)
        size += block.tokens
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def read_parts(
    client: Client,
    chunks: list[str],
    question: str,
    progress: Progress = no_progress,
    cancel: threading.Event | None = None,
) -> list[str]:
    """Notes on each chunk, in order, reading as many at once as the server has slots."""

    def read(chunk: str) -> str:
        check(cancel)
        return client.chat(SYSTEM_MAP, f"{chunk}\n\nQuestion: {question}", NOTE_TOKENS)

    notes = [""] * len(chunks)
    progress(0, len(chunks) + 1, f"reading {len(chunks)} parts")
    with ThreadPoolExecutor(max_workers=client.slots()) as pool:
        futures = {pool.submit(read, chunk): i for i, chunk in enumerate(chunks)}
        try:
            for done, future in enumerate(as_completed(futures), 1):
                notes[futures[future]] = future.result()
                progress(done, len(chunks) + 1, f"read part {done} of {len(chunks)}")
        except BaseException:
            pool.shutdown(cancel_futures=True)
            raise
    return notes


def ask(
    client: Client,
    ws: Workspace,
    question: str,
    paths: list[str],
    progress: Progress = no_progress,
    cancel: threading.Event | None = None,
) -> str:
    if not question.strip():
        raise ToolError("question is empty")
    if not paths:
        raise ToolError("paths is empty: pass the files, directories or globs to read")
    start = time.monotonic()
    files, skipped = ws.expand(paths)
    if not files:
        raise ToolError("no readable files. " + "; ".join(skipped))
    budget = client.context_size() - ANSWER_TOKENS - PROMPT_OVERHEAD_TOKENS
    blocks = [b for p in files for b in file_blocks(client, ws.display(p), read_lines(p), budget)]
    total_tokens = sum(b.tokens for b in blocks)
    if total_tokens > MAX_INPUT_TOKENS:
        raise ToolError(
            f"{len(files)} files, {total_tokens // 1000}K tokens: over the "
            f"{MAX_INPUT_TOKENS // 1000}K limit (~4-5 min of reading). Narrow the paths."
        )
    chunks = pack(blocks, budget)
    calls = 0

    if len(chunks) == 1:
        progress(0, 1, "reading")
        answer = client.chat(SYSTEM_ASK, f"{chunks[0]}\n\nQuestion: {question}", ANSWER_TOKENS)
        calls = 1
    else:
        notes = [
            note
            for note in read_parts(client, chunks, question, progress, cancel)
            if note and NOTHING not in note[: len(NOTHING) + 10]
        ]
        calls += len(chunks)
        check(cancel)
        progress(len(chunks), len(chunks) + 1, "combining")
        if not notes:
            answer = "Nothing relevant found in these files."
        else:
            # Notes are short, but merge in rounds if they still overflow the context.
            while True:
                groups = pack([Block(n, client.count_tokens(n)) for n in notes], budget)
                merged = [
                    client.chat(SYSTEM_REDUCE, f"Notes:\n\n{g}\n\nQuestion: {question}",
                                ANSWER_TOKENS)
                    for g in groups
                ]  # fmt: skip
                calls += len(groups)
                if len(merged) == 1:
                    answer = merged[0]
                    break
                notes = merged

    seconds = time.monotonic() - start
    header = (
        f"[local model {client.model} · {len(files)} files, {total_tokens / 1000:.0f}K"
        f" tokens, {calls} call{'s' if calls != 1 else ''} · {seconds:.0f} s"
        " · smaller model: verify cited lines before relying on them]"
    )
    parts = [header, answer.strip() or "(the local model returned an empty answer)"]
    if skipped:
        parts.append("Skipped: " + "; ".join(skipped))
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- local_search


def windows(lines: list[str]) -> Iterator[tuple[int, int, str]]:
    """Overlapping (start, end, text) line windows of a file, 1-based and inclusive."""
    if not lines:
        return
    start = 0
    while True:
        end = min(start + SEARCH_WINDOW, len(lines))
        yield start + 1, end, "\n".join(lines[start:end])[:SEARCH_CHUNK_CHARS]
        if end == len(lines):
            return
        start += SEARCH_STEP


def normalize(vector: list[float]) -> list[float]:
    norm = sum(x * x for x in vector) ** 0.5 or 1.0
    return [x / norm for x in vector]


def pack_vector(vector: list[float]) -> str:
    return base64.b64encode(array.array("f", normalize(vector)).tobytes()).decode()


def unpack_vector(data: str) -> array.array:
    vector = array.array("f")
    vector.frombytes(base64.b64decode(data))
    return vector


@dataclass
class Hit:
    score: float
    path: str
    start: int
    end: int
    preview: str


class Index:
    """Embeddings of a project's text files, cached on disk and updated incrementally."""

    def __init__(self, ws: Workspace, client: Client, cache_dir: Path = CACHE_DIR) -> None:
        self.ws = ws
        self.client = client
        self.file = cache_dir / f"index-{root_key(ws.root)}.json"
        self.data: dict[str, Any] = {"root": str(ws.root), "model": client.embed_model}
        self.data["files"] = {}
        try:
            loaded = json.loads(self.file.read_text())
            if loaded.get("model") == client.embed_model and loaded.get("root") == str(ws.root):
                self.data = loaded
        except (OSError, ValueError):
            pass

    def update(
        self,
        files: list[Path],
        progress: Progress = no_progress,
        cancel: threading.Event | None = None,
    ) -> tuple[int, list[Path]]:
        """Embed new or changed files, at most SEARCH_MAX_NEW_CHUNKS chunks per call.

        Returns how many files were embedded and the files still waiting. Progress is
        saved after each file, so an interrupted or capped run is not lost.
        """
        entries: dict[str, Any] = self.data["files"]
        gone = [name for name in entries if not (self.ws.root / name).is_file()]
        for name in gone:
            del entries[name]
        stale: list[Path] = []
        for path in files:
            stat = path.stat()
            entry = entries.get(self.ws.display(path))
            if not entry or entry["mtime_ns"] != stat.st_mtime_ns or entry["size"] != stat.st_size:
                stale.append(path)
        done = 0
        embedded = 0
        last_save = time.monotonic()
        for i, path in enumerate(stale):
            parts = list(windows(read_lines(path)))
            if done and done + len(parts) > SEARCH_MAX_NEW_CHUNKS:
                break
            name = self.ws.display(path)
            stat = path.stat()
            chunks: list[list[Any]] = []
            for j in range(0, len(parts), EMBED_BATCH):
                if cancel is not None and cancel.is_set():
                    self.save()  # keep the files finished so far
                    check(cancel)
                progress(done + j, done + len(parts), f"embedding {name}")
                batch = parts[j : j + EMBED_BATCH]
                vectors = self.client.embed([f"{name}\n{text}" for _, _, text in batch])
                chunks += [
                    [start, end, pack_vector(v)]
                    for (start, end, _), v in zip(batch, vectors, strict=True)
                ]
            entries[name] = {"mtime_ns": stat.st_mtime_ns, "size": stat.st_size, "chunks": chunks}
            done += len(parts)
            embedded = i + 1
            if time.monotonic() - last_save > 10:
                self.save()
                last_save = time.monotonic()
        if embedded or gone:
            self.save()
        return embedded, stale[embedded:]

    def save(self) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data))
        tmp.replace(self.file)

    def search(self, query: str, files: list[Path], limit: int) -> list[Hit]:
        names = {self.ws.display(p): p for p in files}
        q = array.array("f", normalize(self.client.embed([SEARCH_QUERY_PREFIX + query])[0]))
        scored: list[tuple[float, str, int, int]] = []
        for name in names:
            for start, end, data in self.data["files"].get(name, {}).get("chunks", []):
                score = sum(a * b for a, b in zip(q, unpack_vector(data), strict=True))
                scored.append((score, name, start, end))
        scored.sort(reverse=True)
        hits: list[Hit] = []
        for score, name, start, end in scored:
            # Overlapping windows of one region would crowd out other results.
            if any(h.path == name and start <= h.end and end >= h.start for h in hits):
                continue
            lines = read_lines(names[name])[start - 1 : end]
            preview = next((ln.strip() for ln in lines if ln.strip()), "")[:100]
            hits.append(Hit(score, name, start, end, preview))
            if len(hits) == limit:
                break
        return hits


def search(
    client: Client,
    ws: Workspace,
    query: str,
    path: str = ".",
    limit: int = 8,
    progress: Progress = no_progress,
    cache_dir: Path = CACHE_DIR,
    cancel: threading.Event | None = None,
) -> str:
    if not query.strip():
        raise ToolError("query is empty")
    start = time.monotonic()
    directory = ws.resolve(path or ".")
    if not directory.is_relative_to(ws.root):
        raise ToolError(f"{path}: search covers the project only")
    files, _ = ws.expand([str(directory)])
    files = [
        p
        for p in files
        if p.stat().st_size <= SEARCH_MAX_FILE_BYTES
        and not any(fnmatch.fnmatch(p.name, g) for g in SEARCH_SKIP_NAMES)
    ]
    if not files:
        raise ToolError(f"{path}: no text files to search")
    # One search per project at a time: they share the index file.
    lock = cache_dir / f"index-{root_key(ws.root)}.lock"
    with queue_lock(lock, "waiting for another search of this project", SEARCH_QUEUE_WAIT,
                    progress, cancel):  # fmt: skip
        index = Index(ws, client, cache_dir)
        embedded, waiting = index.update(files, progress, cancel)
        hits = index.search(query, files, max(1, min(limit, 50)))
    lines = [
        f"[local search · {len(files)} files · {embedded} re-embedded"
        f" · {time.monotonic() - start:.1f} s]"
    ]
    if waiting:
        lines.append(
            f"Index incomplete: {len(waiting)} files not indexed yet, so results cover only "
            "the indexed part. Call again to continue indexing, or pass a narrower path."
        )
    for i, h in enumerate(hits, 1):
        lines.append(f"{i}. {h.path}:{h.start}-{h.end} ({h.score:.2f})  {h.preview}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- local_task


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, check=True, text=True, timeout=60
    ).stdout


def file_state(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return "deleted"
    except OSError:
        return "unreadable"


def dirty_files(root: Path) -> dict[str, str]:
    """Modified, deleted and untracked files (relative paths) with a content fingerprint."""
    out = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    entries = out.split("\0")
    files: dict[str, str] = {}
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        status, name = entry[:2], entry[3:]
        if "R" in status or "C" in status:
            i += 1  # the next entry is the rename/copy source
        files[name] = file_state(root / name)
    return files


def describe_changes(
    root: Path, before: dict[str, str], after: dict[str, str], requested: list[str] | None = None
) -> list[str]:
    changed = sorted(
        {name for name, state in after.items() if before.get(name) != state}
        | {name for name in before if name not in after}  # reverted to the committed version
    )
    tracked = set(git(root, "ls-files", "-z").split("\0"))
    numstat: dict[str, str] = {}
    if changed:
        for line in git(root, "diff", "--numstat", "HEAD", "--", *changed).splitlines():
            added, deleted, name = line.split("\t", 2)
            numstat[name] = f"+{added} -{deleted}"
    lines = []
    for name in changed:
        if "__pycache__" in Path(name).parts:
            continue
        path = root / name
        if name not in tracked:
            count = len(read_lines(path)) if path.is_file() and is_text(path) else 0
            extra = " - NOT REQUESTED" if requested and name not in requested else ""
            lines.append(f"  A {name} (new, {count} lines){extra}")
        elif not path.exists():
            lines.append(f"  D {name}")
        else:
            note = " (already modified before the task; diff includes those changes)"
            lines.append(
                f"  M {name} ({numstat.get(name, 'no diff vs HEAD')})"
                + (note if name in before else "")
            )
    return lines


def tail(text: str, lines: int) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def is_test_file(name: str) -> bool:
    path = Path(name)
    return "tests" in path.parts[:-1] or any(
        fnmatch.fnmatch(path.name, pattern) for pattern in TEST_FILE_PATTERNS
    )


def worker_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith(STRIPPED_ENV_PREFIXES)}


def stop(proc: subprocess.Popen) -> tuple[str, str]:
    """Kill a process and its children (own process group); returns stdout, stderr."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
            return proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            continue
        except ProcessLookupError:
            break
    return proc.communicate()


def run_worker(
    cmd: list[str],
    root: Path,
    timeout: float,
    progress: Progress,
    cancel: threading.Event | None = None,
) -> tuple[str, str, bool]:
    """Run the worker, reporting progress every 15 s; returns stdout, stderr, timed_out.

    The worker is killed on timeout, on cancel (raising Cancelled) or on any error.
    """
    start = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        cwd=root,
        env=worker_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,  # own process group, so a timeout kills its children too
    )
    last = start
    try:
        while True:
            try:
                out, err = proc.communicate(timeout=1)
                return out, err, False
            except subprocess.TimeoutExpired:
                pass
            check(cancel)
            now = time.monotonic()
            if now - start >= timeout:
                break
            if now - last >= 15:
                progress(int(now - start), int(timeout), f"worker running ({now - start:.0f} s)")
                last = now
    except BaseException:
        stop(proc)
        raise
    return *stop(proc), True


def run_tests(command: list[str], root: Path) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            command,
            cwd=root,
            env=worker_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=TEST_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {TEST_TIMEOUT} s"
    except OSError as e:
        return False, f"could not run: {e}"
    return proc.returncode == 0, f"exit {proc.returncode}\n" + tail(
        proc.stdout + proc.stderr, TEST_OUTPUT_LINES
    )


def task(
    ws: Workspace,
    spec: str,
    files: list[str],
    test_command: str,
    model: str,
    timeout_minutes: int = TASK_TIMEOUT // 60,
    progress: Progress = no_progress,
    worker: list[str] | None = None,
    lock_dir: Path = CACHE_DIR,
    protect: list[str] | None = None,
    cancel: threading.Event | None = None,
) -> str:
    if not spec.strip():
        raise ToolError("task is empty")
    try:
        git(ws.root, "rev-parse", "--verify", "HEAD")
    except (subprocess.SubprocessError, OSError) as e:
        raise ToolError(
            f"{ws.root} is not a git repository with a commit: local_task needs git so the "
            "worker's changes can be reviewed and undone"
        ) from e
    names = []
    for spec_path in files:
        path = ws.resolve(spec_path)
        if not path.is_relative_to(ws.root) or is_secret(path):
            raise ToolError(f"{spec_path}: the worker may only change project files")
        names.append(ws.display(path))
    protected = {ws.display(ws.resolve(p)) for p in protect or []}
    protected |= {
        n for n in git(ws.root, "ls-files", "-z", "-co", "--exclude-standard").split("\0")
        if n and is_test_file(n) and n not in names
    }  # fmt: skip
    test_argv = shlex.split(test_command) if test_command.strip() else []
    if test_argv and not shutil.which(test_argv[0]):
        raise ToolError(f"test command not found: {test_argv[0]}")
    timeout = max(1, min(timeout_minutes, TASK_MAX_TIMEOUT // 60)) * 60

    prompt = WORKER_PROMPT.format(
        task=spec.strip(),
        files=(
            f"\nFiles to create or change: {', '.join(names)}. Do not create any other files "
            "(no helper or scratch scripts).\n"
            if names
            else ""
        ),
        tests=(
            f"\nCheck your work by running `{test_command}` and fix failures until it passes.\n"
            if test_argv
            else ""
        ),
    )
    allowed = list(WORKER_TOOLS)
    if test_argv:
        allowed += [f"Bash({test_command})", f"Bash({test_command}:*)"]
    denied = [f"{tool}({name})" for name in sorted(protected) for tool in ("Edit", "Write")]
    cmd = [*(worker or [str(HERE / "claude-local.sh"), "--model", model])]
    cmd += ["-p", prompt, "--output-format", "json", "--permission-mode", "acceptEdits"]
    cmd += ["--strict-mcp-config", "--no-session-persistence"]
    if denied:
        cmd += ["--disallowedTools", *denied]
    cmd += ["--allowedTools", *allowed]

    lock = lock_dir / f"task-{root_key(ws.root)}.lock"
    with queue_lock(lock, "queued behind another local_task in this repository",
                    TASK_QUEUE_WAIT, progress, cancel):  # fmt: skip
        start = time.monotonic()
        before = dirty_files(ws.root)
        saved = {n: (ws.root / n).read_bytes() for n in protected if (ws.root / n).is_file()}
        progress(0, timeout, "starting worker")
        restored = []
        try:
            out, err, timed_out = run_worker(cmd, ws.root, timeout, progress, cancel)
        finally:  # also when cancelled: never leave tests rewritten by the worker
            for name, content in sorted(saved.items()):
                path = ws.root / name
                if not path.is_file() or path.read_bytes() != content:
                    path.write_bytes(content)
                    restored.append(name)
        minutes = (time.monotonic() - start) / 60
        changes = describe_changes(ws.root, before, dirty_files(ws.root), names)
        if test_argv:
            progress(timeout, timeout, "running tests")
            passed, test_out = run_tests(test_argv, ws.root)

    try:
        result = json.loads(out)
        summary = str(result.get("result") or "").strip()
        turns = result.get("num_turns", "?")
        denials = sorted(
            {
                f"{d.get('tool_name')}: "
                + str((d.get("tool_input") or {}).get("file_path")
                      or (d.get("tool_input") or {}).get("command") or "?")[:120]
                for d in result.get("permission_denials") or []
            }
        )  # fmt: skip
        status = "error" if result.get("is_error") else "finished"
    except ValueError:
        denials = []
        # Not JSON: crashed or killed. Show stderr minus claude-local's start-up lines.
        noise = ("claude-local:", "⚠", "[claude-code:")
        lines = [ln for ln in (err or out).splitlines() if not ln.startswith(noise)]
        summary = tail("\n".join(lines), 15)
        turns = "?"
        status = "failed to start or crashed"
    if timed_out:
        status = f"stopped at the {timeout // 60}-min limit"

    parts = [f"[local task · {model} · {minutes:.1f} min · {turns} turns · worker {status}]"]
    if restored:
        parts.append(
            "WARNING: the worker changed protected files; the server restored them: "
            + ", ".join(restored)
            + ". Treat its result with extra suspicion."
        )
    if test_argv:
        verdict = "PASSED" if passed else "FAILED"
        parts.append(f"Tests (`{test_command}`, run by the server): {verdict}, {test_out}")
    else:
        parts.append("Tests: none given, so nothing was verified.")
    parts.append("Changed files:\n" + ("\n".join(changes) if changes else "  (none)"))
    if denials:
        parts.append(
            "Refused actions (outside the project, protected or not allowed):\n"
            + "\n".join(f"  {d}" for d in denials)
        )
    if len(summary) > TASK_SUMMARY_CHARS:
        summary = summary[:TASK_SUMMARY_CHARS] + " [...]"
    parts.append("Worker summary:\n" + (summary or "(none)"))
    parts.append("Review the diff (git diff) before building on it.")
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- MCP server


class Server:
    """MCP over stdio: newline-delimited JSON-RPC 2.0, tools only.

    Tool calls run on threads, so a long call (Claude Code moves calls over two minutes
    to the background) doesn't hold up the next one. A call stops when Claude Code
    sends notifications/cancelled for it, and gets no response then, per the spec.
    """

    def __init__(
        self, client: Client, ws: Workspace, out: IO[str], cache_dir: Path = CACHE_DIR
    ) -> None:
        self.client = client
        self.ws = ws
        self.out = out
        self.cache_dir = cache_dir
        self._out_lock = threading.Lock()
        self._calls_lock = threading.Lock()
        self._calls: dict[str | int, threading.Event] = {}  # running tool calls by request id

    def send(self, message: dict) -> None:
        line = json.dumps(message) + "\n"
        with self._out_lock:
            self.out.write(line)
            self.out.flush()

    def serve(self, stdin: IO[str]) -> None:
        with ThreadPoolExecutor(MAX_CONCURRENT_CALLS, thread_name_prefix="tool") as pool:
            try:
                for line in stdin:
                    if not line.strip():
                        continue
                    try:
                        message = json.loads(line)
                    except ValueError:
                        self.send(error_response(None, -32700, "parse error"))
                        continue
                    if self.is_tool_call(message):
                        self.start_call(pool, message)
                        continue
                    response = self.handle(message)
                    if response is not None:
                        self.send(response)
            finally:
                # Client gone: stop running calls (kills workers) and let threads finish.
                with self._calls_lock:
                    for event in self._calls.values():
                        event.set()

    @staticmethod
    def is_tool_call(message: Any) -> bool:
        return (
            isinstance(message, dict)
            and message.get("jsonrpc") == "2.0"
            and message.get("method") == "tools/call"
            and isinstance(message.get("id"), str | int)
        )

    def start_call(self, pool: ThreadPoolExecutor, message: dict) -> None:
        msg_id = message["id"]
        cancel = threading.Event()
        with self._calls_lock:
            self._calls[msg_id] = cancel

        def run() -> None:
            try:
                response = self.handle(message)
            finally:
                with self._calls_lock:
                    self._calls.pop(msg_id, None)
            if response is not None and not cancel.is_set():
                self.send(response)

        pool.submit(run)

    def cancel(self, params: dict) -> None:
        request_id = params.get("requestId")
        if not isinstance(request_id, str | int):
            return
        with self._calls_lock:
            event = self._calls.get(request_id)
        if event is not None:
            log.info("cancelling request %s: %s", request_id, params.get("reason"))
            event.set()

    def handle(self, message: Any) -> dict | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return error_response(None, -32600, "invalid request")
        method = message.get("method")
        params = message.get("params") or {}
        if "id" not in message:
            if method == "notifications/cancelled":
                self.cancel(params if isinstance(params, dict) else {})
            return None  # notifications get no answer
        msg_id = message["id"]
        if method == "initialize":
            requested = params.get("protocolVersion")
            version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return result_response(
                msg_id,
                {
                    "protocolVersion": version,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "local-llm", "version": SERVER_VERSION},
                    "instructions": INSTRUCTIONS,
                },
            )
        if method == "ping":
            return result_response(msg_id, {})
        if method == "tools/list":
            return result_response(msg_id, {"tools": TOOLS})
        if method == "tools/call":
            with self._calls_lock:
                cancel = self._calls.get(msg_id) if isinstance(msg_id, str | int) else None
            try:
                text, is_error = self.call_tool(params, cancel)
            except Cancelled:
                return None
            return result_response(
                msg_id, {"content": [{"type": "text", "text": text}], "isError": is_error}
            )
        return error_response(msg_id, -32601, f"method not found: {method}")

    def call_tool(self, params: dict, cancel: threading.Event | None = None) -> tuple[str, bool]:
        name = params.get("name")
        args = params.get("arguments") or {}
        token = (params.get("_meta") or {}).get("progressToken")

        def progress(done: int, total: int, message: str) -> None:
            if token is not None:
                self.send(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/progress",
                        "params": {
                            "progressToken": token,
                            "progress": done,
                            "total": total,
                            "message": message,
                        },
                    }
                )

        try:
            if name == "local_ask":
                return ask(
                    self.client, self.ws, str(args.get("question", "")),
                    [str(p) for p in args.get("paths") or []], progress, cancel,
                ), False  # fmt: skip
            if name == "local_task":
                return task(
                    self.ws, str(args.get("task", "")),
                    [str(p) for p in args.get("files") or []],
                    str(args.get("test_command") or ""), self.client.model,
                    int(args.get("timeout_minutes") or TASK_TIMEOUT // 60),
                    progress, lock_dir=self.cache_dir,
                    protect=[str(p) for p in args.get("protect") or []], cancel=cancel,
                ), False  # fmt: skip
            if name == "local_search":
                return search(
                    self.client, self.ws, str(args.get("query", "")),
                    str(args.get("path") or "."), int(args.get("limit") or 8),
                    progress, self.cache_dir, cancel,
                ), False  # fmt: skip
            return f"unknown tool: {name}", True
        except Cancelled:
            raise
        except ToolError as e:
            return str(e), True
        except Exception as e:
            log.exception("tool %s failed", name)
            return f"internal error in {name}: {e}", True


def result_response(msg_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def error_response(msg_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


# --------------------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="MCP server that hands bulk reading in Claude Code to the local model.",
        epilog="Register once: claude mcp add -s user local-llm -- /path/to/llm_mcp.py",
    )
    parser.add_argument("--url", default=os.environ.get("LOCAL_LLM_URL", DEFAULT_URL))
    parser.add_argument("--model", default=os.environ.get("LOCAL_LLM_MODEL", "qwen3-coder"))
    parser.add_argument("--embed-model", default="embed")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="project directory")
    parser.add_argument(
        "--allow",
        type=Path,
        action="append",
        default=[Path(tempfile.gettempdir())],
        help="extra directory files may be read from (default: the temp directory)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="serve MCP over stdio (default)")
    p_ask = sub.add_parser("ask", help="run local_ask from the shell")
    p_ask.add_argument("question")
    p_ask.add_argument("paths", nargs="+")
    p_search = sub.add_parser("search", help="run local_search from the shell")
    p_search.add_argument("query")
    p_search.add_argument("path", nargs="?", default=".")
    p_search.add_argument("-n", "--limit", type=int, default=8)
    p_task = sub.add_parser("task", help="run local_task from the shell")
    p_task.add_argument("task")
    p_task.add_argument("--file", action="append", default=[], dest="files")
    p_task.add_argument(
        "--protect", action="append", default=[], help="file the worker must not change"
    )
    p_task.add_argument("--test", default="", help="test command run by the worker and after")
    p_task.add_argument("--timeout", type=int, default=TASK_TIMEOUT // 60, help="minutes")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO if args.verbose else logging.WARNING,
        format="llm-mcp: %(message)s",
    )
    client = Client(args.url, args.model, args.embed_model)
    ws = Workspace(args.root, args.allow)

    def show(done: int, total: int, message: str) -> None:
        log.info("%s (%d/%d)", message, done, total)

    try:
        if args.command == "ask":
            print(ask(client, ws, args.question, args.paths, show))
        elif args.command == "search":
            print(search(client, ws, args.query, args.path, args.limit, show))
        elif args.command == "task":
            print(
                task(ws, args.task, args.files, args.test, client.model, args.timeout, show,
                     protect=args.protect)
            )  # fmt: skip
        else:
            Server(client, ws, sys.stdout).serve(sys.stdin)
    except ToolError as e:
        log.error("%s", e)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
