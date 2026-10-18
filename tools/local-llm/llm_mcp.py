#!/usr/bin/env python3
"""MCP server that hands bulk reading in Claude Code to the local model.

Claude passes file paths, this server reads the files itself and asks the local
model (via llama-swap), and only the short answer goes back into Claude's
context. That is where the tokens are saved.

Tools:
  local_ask     answer a question about files, directories or globs
  local_search  find code by meaning, using the embedding model

Only files under the project directory (the server's working directory, or
--root) and the system temp directory (or --allow DIRs) are read. Names that look
like secrets (.env, keys, credentials) are always skipped.

  llm_mcp.py                       # serve MCP over stdio (what Claude Code runs)
  llm_mcp.py ask QUESTION PATH...  # same as the tool, from a shell
  llm_mcp.py search QUERY [PATH]
"""

from __future__ import annotations

import argparse
import array
import base64
import fnmatch
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

log = logging.getLogger("llm-mcp")

DEFAULT_URL = "http://127.0.0.1:8080"
CACHE_DIR = (
    Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "legiontools7a" / "llm-mcp"
)
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SERVER_VERSION = "0.1.0"

CHARS_PER_TOKEN = 3  # conservative for code; the real ratio is ~3.5-4
MAX_FILE_BYTES = 2_000_000
MAX_INPUT_TOKENS = 250_000  # ~4 min of prompt processing; beyond that, narrow the paths
ANSWER_TOKENS = 1500
NOTE_TOKENS = 1024
PROMPT_OVERHEAD_TOKENS = 1024
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

INSTRUCTIONS = """Local model on this machine for bulk reading. Pass file paths, directories or
globs: the server reads the files itself, so their contents never enter your context.
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


Progress = Callable[[int, int, str], None]


def no_progress(done: int, total: int, message: str) -> None:
    pass


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
        self._ctx: int | None = None

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

    def context_size(self) -> int:
        """Context the chat model was started with (loads the model if needed)."""
        if self._ctx is None:
            props = self._request(f"/upstream/{self.model}/props")
            self._ctx = int(props["default_generation_settings"]["n_ctx"])
        return self._ctx

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


def pack(blocks: list[str], budget: int) -> list[str]:
    """Group blocks into chunks of at most `budget` characters."""
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for block in blocks:
        if current and size + len(block) + 2 > budget:
            chunks.append("\n\n".join(current))
            current, size = [], 0
        current.append(block)
        size += len(block) + 2
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def ask(
    client: Client,
    ws: Workspace,
    question: str,
    paths: list[str],
    progress: Progress = no_progress,
) -> str:
    if not question.strip():
        raise ToolError("question is empty")
    if not paths:
        raise ToolError("paths is empty: pass the files, directories or globs to read")
    start = time.monotonic()
    files, skipped = ws.expand(paths)
    if not files:
        raise ToolError("no readable files. " + "; ".join(skipped))
    contents = [(ws.display(p), read_lines(p)) for p in files]
    total_chars = sum(len(line) + 8 for _, lines in contents for line in lines)
    if total_chars // CHARS_PER_TOKEN > MAX_INPUT_TOKENS:
        raise ToolError(
            f"{len(files)} files, about {total_chars // CHARS_PER_TOKEN // 1000}K tokens: "
            f"over the {MAX_INPUT_TOKENS // 1000}K limit. Narrow the paths."
        )

    budget = (client.context_size() - ANSWER_TOKENS - PROMPT_OVERHEAD_TOKENS) * CHARS_PER_TOKEN
    blocks = [b for name, lines in contents for b in segments(name, lines, budget)]
    chunks = pack(blocks, budget)
    calls = 0

    if len(chunks) == 1:
        progress(0, 1, "reading")
        answer = client.chat(SYSTEM_ASK, f"{chunks[0]}\n\nQuestion: {question}", ANSWER_TOKENS)
        calls = 1
    else:
        notes: list[str] = []
        for i, chunk in enumerate(chunks):
            progress(i, len(chunks) + 1, f"reading part {i + 1} of {len(chunks)}")
            note = client.chat(SYSTEM_MAP, f"{chunk}\n\nQuestion: {question}", NOTE_TOKENS)
            calls += 1
            if note and NOTHING not in note[: len(NOTHING) + 10]:
                notes.append(note)
        progress(len(chunks), len(chunks) + 1, "combining")
        if not notes:
            answer = "Nothing relevant found in these files."
        else:
            # Notes are short, but merge in rounds if they still overflow the context.
            while True:
                groups = pack(notes, budget)
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
    ktokens = total_chars // CHARS_PER_TOKEN // 1000
    header = (
        f"[local model {client.model} · {len(files)} files, ~{ktokens}K"
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
        key = hashlib.sha256(str(ws.root).encode()).hexdigest()[:16]
        self.file = cache_dir / f"index-{key}.json"
        self.data: dict[str, Any] = {"root": str(ws.root), "model": client.embed_model}
        self.data["files"] = {}
        try:
            loaded = json.loads(self.file.read_text())
            if loaded.get("model") == client.embed_model and loaded.get("root") == str(ws.root):
                self.data = loaded
        except (OSError, ValueError):
            pass

    def update(self, files: list[Path], progress: Progress = no_progress) -> int:
        """Re-embed new or changed files; returns how many files were embedded."""
        entries: dict[str, Any] = self.data["files"]
        gone = [name for name in entries if not (self.ws.root / name).is_file()]
        for name in gone:
            del entries[name]
        stale: list[tuple[str, Path]] = []
        for path in files:
            name = self.ws.display(path)
            stat = path.stat()
            entry = entries.get(name)
            if not entry or entry["mtime_ns"] != stat.st_mtime_ns or entry["size"] != stat.st_size:
                stale.append((name, path))
        pending: list[tuple[str, int, int, str]] = []
        for name, path in stale:
            stat = path.stat()
            entries[name] = {"mtime_ns": stat.st_mtime_ns, "size": stat.st_size, "chunks": []}
            pending += [(name, s, e, t) for s, e, t in windows(read_lines(path))]
        for i in range(0, len(pending), EMBED_BATCH):
            progress(i, len(pending), f"embedding {len(pending)} chunks")
            batch = pending[i : i + EMBED_BATCH]
            vectors = self.client.embed([f"{name}\n{text}" for name, _, _, text in batch])
            for (name, start, end, _), vector in zip(batch, vectors, strict=True):
                entries[name]["chunks"].append([start, end, pack_vector(vector)])
        if stale or gone:
            self.save()
        return len(stale)

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
) -> str:
    if not query.strip():
        raise ToolError("query is empty")
    start = time.monotonic()
    directory = ws.resolve(path or ".")
    if not directory.is_relative_to(ws.root):
        raise ToolError(f"{path}: search covers the project only")
    files, _ = ws.expand([str(directory)])
    files = [p for p in files if not any(fnmatch.fnmatch(p.name, g) for g in SEARCH_SKIP_NAMES)]
    if not files:
        raise ToolError(f"{path}: no text files to search")
    index = Index(ws, client, cache_dir)
    embedded = index.update(files, progress)
    hits = index.search(query, files, max(1, min(limit, 50)))
    lines = [
        f"[local search · {len(files)} files · {embedded} re-embedded"
        f" · {time.monotonic() - start:.1f} s]"
    ]
    for i, h in enumerate(hits, 1):
        lines.append(f"{i}. {h.path}:{h.start}-{h.end} ({h.score:.2f})  {h.preview}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- MCP server


class Server:
    """MCP over stdio: newline-delimited JSON-RPC 2.0, tools only."""

    def __init__(
        self, client: Client, ws: Workspace, out: IO[str], cache_dir: Path = CACHE_DIR
    ) -> None:
        self.client = client
        self.ws = ws
        self.out = out
        self.cache_dir = cache_dir

    def send(self, message: dict) -> None:
        self.out.write(json.dumps(message) + "\n")
        self.out.flush()

    def serve(self, stdin: IO[str]) -> None:
        for line in stdin:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except ValueError:
                self.send(error_response(None, -32700, "parse error"))
                continue
            response = self.handle(message)
            if response is not None:
                self.send(response)

    def handle(self, message: Any) -> dict | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return error_response(None, -32600, "invalid request")
        method = message.get("method")
        if "id" not in message:
            return None  # notification (initialized, cancelled, ...): nothing to answer
        msg_id = message["id"]
        params = message.get("params") or {}
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
            text, is_error = self.call_tool(params)
            return result_response(
                msg_id, {"content": [{"type": "text", "text": text}], "isError": is_error}
            )
        return error_response(msg_id, -32601, f"method not found: {method}")

    def call_tool(self, params: dict) -> tuple[str, bool]:
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
                    [str(p) for p in args.get("paths") or []], progress,
                ), False  # fmt: skip
            if name == "local_search":
                return search(
                    self.client, self.ws, str(args.get("query", "")),
                    str(args.get("path") or "."), int(args.get("limit") or 8),
                    progress, self.cache_dir,
                ), False  # fmt: skip
            return f"unknown tool: {name}", True
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
