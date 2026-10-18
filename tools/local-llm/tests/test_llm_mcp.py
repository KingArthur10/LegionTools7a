import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "llm_mcp", Path(__file__).parent.parent / "llm_mcp.py"
)
mcp = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = mcp
_spec.loader.exec_module(mcp)


class FakeClient:
    """Records chat prompts; embeds text as counts of a few keywords."""

    model = "fake"
    embed_model = "fake-embed"
    KEYWORDS = ("alpha", "beta", "gamma")

    def __init__(self, ctx: int = 8192, answers: list[str] | None = None) -> None:
        self.ctx = ctx
        self.answers = list(answers or [])
        self.prompts: list[tuple[str, str]] = []
        self.embedded: list[str] = []

    def context_size(self) -> int:
        return self.ctx

    def chat(self, system, prompt, max_tokens):
        self.prompts.append((system, prompt))
        return self.answers.pop(0) if self.answers else "answer"

    def embed(self, texts):
        self.embedded += texts
        return [[t.count(k) + 0.01 for k in self.KEYWORDS] for t in texts]


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("def alpha():\n    return 1\n")
    (root / "src" / "b.py").write_text("def beta():\n    return 2\n")
    (root / ".env").write_text("TOKEN=hunter2\n")
    (root / "blob.bin").write_bytes(b"\0\1\2")
    return root


# --------------------------------------------------------------------------- paths


def test_resolve_rejects_paths_outside_root(project, tmp_path):
    ws = mcp.Workspace(project)
    (tmp_path / "outside.txt").write_text("x")
    with pytest.raises(mcp.ToolError, match="outside"):
        ws.resolve("../outside.txt")
    with pytest.raises(mcp.ToolError, match="outside"):
        ws.resolve(str(tmp_path / "outside.txt"))


def test_resolve_rejects_symlink_escape(project, tmp_path):
    (tmp_path / "outside.txt").write_text("x")
    (project / "link.txt").symlink_to(tmp_path / "outside.txt")
    with pytest.raises(mcp.ToolError, match="outside"):
        mcp.Workspace(project).resolve("link.txt")


def test_extra_allowed_directory(project, tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "run.log").write_text("ok\n")
    files, _ = mcp.Workspace(project, [logs]).expand([str(logs / "run.log")])
    assert files == [logs / "run.log"]


def test_expand_directory_skips_secrets_and_binaries(project):
    files, skipped = mcp.Workspace(project).expand(["."])
    names = sorted(f.name for f in files)
    assert names == ["a.py", "b.py"]
    assert any(".env" in s and "secret" in s for s in skipped)


def test_expand_explicit_secret_is_refused(project):
    files, skipped = mcp.Workspace(project).expand([".env"])
    assert files == []
    assert "secret" in skipped[0]


def test_expand_glob_and_missing(project):
    files, skipped = mcp.Workspace(project).expand(["src/*.py", "nope.txt", "*.md"])
    assert [f.name for f in files] == ["a.py", "b.py"]
    assert "nope.txt: not found" in skipped
    assert "*.md: no matches" in skipped


def test_expand_glob_cannot_escape(project):
    with pytest.raises(mcp.ToolError, match="relative"):
        mcp.Workspace(project).expand(["/etc/*.conf"])


def test_is_secret():
    assert mcp.is_secret(Path("/x/.env.local"))
    assert mcp.is_secret(Path("/x/aws_credentials.json"))
    assert mcp.is_secret(Path("/home/u/.ssh/config"))
    assert not mcp.is_secret(Path("/x/environment.py"))


# --------------------------------------------------------------------------- chunking


def test_segments_number_lines_and_split():
    lines = [f"line {i}" for i in range(1, 101)]
    blocks = list(mcp.segments("f.txt", lines, budget=400))
    assert len(blocks) > 1
    assert blocks[0].startswith("=== f.txt (lines 1-")
    assert "1: line 1" in blocks[0]
    assert blocks[-1].split("\n")[0].endswith("of 100) ===")
    assert all(len(b) <= 400 for b in blocks)
    numbered = [ln for b in blocks for ln in b.split("\n")[1:]]
    assert numbered == [f"{i}: line {i}" for i in range(1, 101)]


def test_segments_empty_file():
    assert list(mcp.segments("e.txt", [], 400)) == ["=== e.txt (empty file) ==="]


def test_pack_respects_budget():
    chunks = mcp.pack(["a" * 40, "b" * 40, "c" * 40], budget=90)
    assert chunks == ["a" * 40 + "\n\n" + "b" * 40, "c" * 40]


# --------------------------------------------------------------------------- local_ask


def test_ask_single_chunk(project):
    client = FakeClient(answers=["alpha returns 1 (src/a.py:2)"])
    out = mcp.ask(client, mcp.Workspace(project), "what does alpha return?", ["src"])
    assert len(client.prompts) == 1
    system, prompt = client.prompts[0]
    assert system == mcp.SYSTEM_ASK
    assert "=== src/a.py (lines 1-2 of 2) ===" in prompt
    assert prompt.endswith("Question: what does alpha return?")
    assert out.startswith("[local model fake · 2 files")
    assert "src/a.py:2" in out


def test_ask_map_reduce_drops_irrelevant_parts(project):
    big = project / "big.log"
    big.write_text("\n".join(f"event {i} " + "x" * 50 for i in range(400)))
    client = FakeClient(ctx=3000)
    answers = iter(["note about big.log:7"] + [mcp.NOTHING] * 50)
    client.chat = lambda s, p, m: (
        client.prompts.append((s, p)) or ("final" if s == mcp.SYSTEM_REDUCE else next(answers))
    )
    out = mcp.ask(client, mcp.Workspace(project), "what happened?", ["big.log"])
    maps = [p for s, p in client.prompts if s == mcp.SYSTEM_MAP]
    reduces = [p for s, p in client.prompts if s == mcp.SYSTEM_REDUCE]
    assert len(maps) > 1
    assert len(reduces) == 1
    assert "note about big.log:7" in reduces[0]
    assert mcp.NOTHING not in reduces[0]
    assert "final" in out


def test_ask_nothing_relevant(project):
    big = project / "big.log"
    big.write_text("\n".join("x" * 60 for _ in range(400)))
    client = FakeClient(ctx=3000)
    client.chat = lambda s, p, m: mcp.NOTHING
    out = mcp.ask(client, mcp.Workspace(project), "q", ["big.log"])
    assert "Nothing relevant" in out


def test_ask_errors(project):
    ws = mcp.Workspace(project)
    with pytest.raises(mcp.ToolError, match="empty"):
        mcp.ask(FakeClient(), ws, " ", ["src"])
    with pytest.raises(mcp.ToolError, match="no readable files"):
        mcp.ask(FakeClient(), ws, "q", [".env"])


def test_ask_refuses_oversized_input(project, monkeypatch):
    monkeypatch.setattr(mcp, "MAX_INPUT_TOKENS", 5)
    with pytest.raises(mcp.ToolError, match="Narrow the paths"):
        mcp.ask(FakeClient(), mcp.Workspace(project), "q", ["src"])


# --------------------------------------------------------------------------- local_search


def test_windows_overlap():
    lines = [str(i) for i in range(1, 71)]
    assert [(s, e) for s, e, _ in mcp.windows(lines)] == [(1, 40), (31, 70)]
    assert list(mcp.windows([])) == []


def test_search_ranks_and_caches(project, tmp_path):
    cache = tmp_path / "cache"
    ws = mcp.Workspace(project)
    client = FakeClient()
    out = mcp.search(client, ws, "beta beta", cache_dir=cache)
    lines = out.splitlines()
    assert "2 re-embedded" in lines[0]
    assert lines[1].startswith("1. src/b.py:1-2")

    client = FakeClient()
    out = mcp.search(client, ws, "alpha", cache_dir=cache)
    assert "0 re-embedded" in out
    assert out.splitlines()[1].startswith("1. src/a.py:1-2")
    assert len(client.embedded) == 1  # only the query


def test_search_reembeds_changed_and_drops_deleted(project, tmp_path):
    cache = tmp_path / "cache"
    ws = mcp.Workspace(project)
    mcp.search(FakeClient(), ws, "x", cache_dir=cache)
    (project / "src" / "a.py").write_text("def gamma():\n    return 3\n")
    (project / "src" / "b.py").unlink()
    out = mcp.search(FakeClient(), ws, "gamma", cache_dir=cache)
    assert "1 files · 1 re-embedded" in out
    assert "b.py" not in out
    index = json.loads(next(cache.glob("index-*.json")).read_text())
    assert sorted(index["files"]) == ["src/a.py"]


def test_search_outside_project(project, tmp_path):
    with pytest.raises(mcp.ToolError, match="project only"):
        mcp.search(FakeClient(), mcp.Workspace(project, [tmp_path]), "q", str(tmp_path))


# --------------------------------------------------------------------------- MCP protocol


def rpc(server, method, params=None, msg_id=1):
    message = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if msg_id is not None:
        message["id"] = msg_id
    return server.handle(message)


@pytest.fixture
def server(project, tmp_path):
    return mcp.Server(FakeClient(), mcp.Workspace(project), io.StringIO(), tmp_path / "c")


def test_initialize_negotiates_version(server):
    result = rpc(server, "initialize", {"protocolVersion": "2025-06-18"})["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"] == {"tools": {}}
    assert "local_ask" in result["instructions"]
    newer = rpc(server, "initialize", {"protocolVersion": "2099-01-01"})["result"]
    assert newer["protocolVersion"] == mcp.PROTOCOL_VERSIONS[0]


def test_notifications_get_no_response(server):
    assert rpc(server, "notifications/initialized", msg_id=None) is None


def test_tools_list(server):
    tools = rpc(server, "tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == ["local_ask", "local_search"]


def test_unknown_method_and_invalid_request(server):
    assert rpc(server, "resources/list")["error"]["code"] == -32601
    assert server.handle(["not", "a", "dict"])["error"]["code"] == -32600


def test_tools_call_success_and_error(server):
    ok = rpc(server, "tools/call", {"name": "local_ask", "arguments": {
        "question": "q", "paths": ["src/a.py"]}})["result"]  # fmt: skip
    assert ok["isError"] is False
    assert ok["content"][0]["type"] == "text"
    bad = rpc(server, "tools/call", {"name": "local_ask", "arguments": {
        "question": "q", "paths": ["../x"]}})["result"]  # fmt: skip
    assert bad["isError"] is True
    assert "outside" in bad["content"][0]["text"]
    unknown = rpc(server, "tools/call", {"name": "nope"})["result"]
    assert unknown["isError"] is True


def test_progress_notifications_sent_with_token(server):
    rpc(server, "tools/call", {"name": "local_ask", "_meta": {"progressToken": "t1"},
        "arguments": {"question": "q", "paths": ["src"]}})  # fmt: skip
    sent = [json.loads(line) for line in server.out.getvalue().splitlines()]
    assert sent and all(m["method"] == "notifications/progress" for m in sent)
    assert sent[0]["params"]["progressToken"] == "t1"


def test_serve_loop_parse_error_and_responses(project, tmp_path):
    out = io.StringIO()
    server = mcp.Server(FakeClient(), mcp.Workspace(project), out, tmp_path / "c")
    stdin = io.StringIO(
        "not json\n\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"}) + "\n"
    )  # fmt: skip
    server.serve(stdin)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert replies[0]["error"]["code"] == -32700
    assert replies[1] == {"jsonrpc": "2.0", "id": 7, "result": {}}
    assert len(replies) == 2
