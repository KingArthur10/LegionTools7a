import importlib.util
import io
import json
import subprocess
import sys
import textwrap
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

    def count_tokens(self, text):
        # Digits cost a token each (like Qwen's tokenizer); other text ~4 chars/token.
        digits = sum(c.isdigit() for c in text)
        return digits + (len(text) - digits) // 4

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


def test_pack_respects_token_budget():
    blocks = [mcp.Block("a", 40), mcp.Block("b", 40), mcp.Block("c", 40)]
    assert mcp.pack(blocks, budget=90) == ["a\n\nb", "c"]


def test_file_blocks_follow_token_density():
    client = FakeClient()
    prose = [f"word {'x' * 70}" for _ in range(200)]
    digits = ["2026101208000" + "1234567890" * 6 for _ in range(200)]
    prose_blocks = mcp.file_blocks(client, "p.txt", prose, budget=1000)
    digit_blocks = mcp.file_blocks(client, "d.log", digits, budget=1000)
    assert len(digit_blocks) > 2 * len(prose_blocks)
    for b in prose_blocks + digit_blocks:
        assert client.count_tokens(b.text) <= 1000


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


def test_search_caps_new_embeddings_and_resumes(project, tmp_path, monkeypatch):
    monkeypatch.setattr(mcp, "SEARCH_MAX_NEW_CHUNKS", 2)
    for i in range(5):
        (project / "src" / f"m{i}.py").write_text(f"def f{i}():\n    pass\n")
    cache = tmp_path / "cache"
    ws = mcp.Workspace(project)
    out = mcp.search(FakeClient(), ws, "x", cache_dir=cache)
    assert "2 re-embedded" in out
    assert "Index incomplete: 5 files" in out
    out = mcp.search(FakeClient(), ws, "x", cache_dir=cache)
    assert "Index incomplete: 3 files" in out
    for _ in range(2):
        out = mcp.search(FakeClient(), ws, "x", cache_dir=cache)
    assert "Index incomplete" not in out


def test_search_skips_large_files(project, tmp_path, monkeypatch):
    monkeypatch.setattr(mcp, "SEARCH_MAX_FILE_BYTES", 100)
    (project / "src" / "vendor.h").write_text("alpha\n" * 50)
    out = mcp.search(FakeClient(), mcp.Workspace(project), "alpha", cache_dir=tmp_path / "c")
    assert "vendor.h" not in out
    assert "2 files" in out


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
    assert [t["name"] for t in tools] == ["local_task", "local_ask", "local_search"]


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


# --------------------------------------------------------------------------- local_task

FAKE_WORKER = textwrap.dedent("""
    import json, os, sys, time
    from pathlib import Path
    prompt = sys.argv[sys.argv.index("-p") + 1]
    mode = os.environ.get("FAKE_MODE", "ok")
    Path("env.txt").write_text(" ".join(sorted(k for k in os.environ if "CLAUDE" in k)))
    if mode == "sleep":
        time.sleep(60)
    if mode == "crash":
        sys.exit("boom")
    if mode == "cheat":
        Path("test_calc.py").write_text("def test_nothing():\\n    pass\\n")
    if "--disallowedTools" in sys.argv:
        Path("denied.txt").write_text(sys.argv[sys.argv.index("--disallowedTools") + 1])
    Path("calc.py").write_text("def add(a, b):\\n    return a + b\\n")
    Path("README.md").write_text("changed\\n")
    Path("scratch.py").write_text("print(1)\\n")
    print(json.dumps({"result": "Added add() in calc.py. PROMPT=" + prompt[:40],
                      "num_turns": 4, "is_error": False,
                      "permission_denials": [{"tool_name": "Write",
                                              "tool_input": {"file_path": "/etc/x"}}]}))
""")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    for cmd in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(root), *cmd], check=True)
    (root / "README.md").write_text("hello\n")
    (root / "notes.txt").write_text("draft\n")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True)
    (root / "notes.txt").write_text("edited by the user\n")  # dirty before the task
    (root / "test_calc.py").write_text("from calc import add\ndef test_add():\n"
                                       "    assert add(2, 3) == 5\n")  # fmt: skip
    worker = tmp_path / "worker.py"
    worker.write_text(FAKE_WORKER)
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent")
    return root, [sys.executable, str(worker)]


def run_task(repo, tmp_path, **kw):
    root, worker = repo
    kw.setdefault("files", ["calc.py"])
    kw.setdefault("test_command", f"{sys.executable} -m pytest -q test_calc.py")
    return mcp.task(mcp.Workspace(root), kw.pop("spec", "add add()"), model="fake",
                    worker=worker, lock_dir=tmp_path / "lock", **kw)  # fmt: skip


def test_task_reports_tests_and_changes(repo, tmp_path):
    out = run_task(repo, tmp_path)
    assert "worker finished" in out
    assert "4 turns" in out
    assert "PASSED" in out
    assert "A calc.py (new, 2 lines)" in out
    assert "M README.md (+1 -1)" in out
    assert "A scratch.py (new, 1 lines) - NOT REQUESTED" in out
    changes = out.split("Changed files:")[1].split("Worker summary:")[0]
    assert "notes.txt" not in changes  # dirty before, untouched by the worker
    assert "test_calc.py" not in changes
    assert "Added add() in calc.py" in out


def test_task_prompt_lists_files_and_tests(repo, tmp_path):
    out = run_task(repo, tmp_path)
    assert "PROMPT=You are implementing one well-specified" in out


def test_task_failed_tests(repo, tmp_path):
    (repo[0] / "test_calc.py").write_text("def test_fail():\n    assert False\n")
    assert "FAILED, exit 1" in run_task(repo, tmp_path)


def test_task_strips_claude_session_env(repo, tmp_path):
    run_task(repo, tmp_path)
    assert (repo[0] / "env.txt").read_text() == ""


def test_run_worker_timeout_kills_process(tmp_path):
    start = mcp.time.monotonic()
    cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
    _, _, timed_out = mcp.run_worker(cmd, tmp_path, 1, mcp.no_progress)
    assert timed_out
    assert mcp.time.monotonic() - start < 15


def test_task_worker_crash(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "crash")
    out = run_task(repo, tmp_path)
    assert "failed to start or crashed" in out
    assert "boom" in out
    assert "FAILED" in out  # calc.py was never written


def test_task_without_tests_says_unverified(repo, tmp_path):
    assert "nothing was verified" in run_task(repo, tmp_path, test_command="")


def test_task_rejects_files_outside_project(repo, tmp_path):
    with pytest.raises(mcp.ToolError, match=r"outside|only change project files"):
        run_task(repo, tmp_path, files=[str(tmp_path / "elsewhere.py")])


def test_task_needs_git(project, tmp_path):
    with pytest.raises(mcp.ToolError, match="not a git repository"):
        mcp.task(mcp.Workspace(project), "x", [], "", "fake", lock_dir=tmp_path)


def test_task_one_at_a_time(repo, tmp_path):
    lock_dir = tmp_path / "lock"
    lock_dir.mkdir()
    with (lock_dir / "task.lock").open("w") as held:
        mcp.fcntl.flock(held, mcp.fcntl.LOCK_EX | mcp.fcntl.LOCK_NB)
        with pytest.raises(mcp.ToolError, match="another local_task"):
            run_task(repo, tmp_path)


def test_dirty_files_handles_renames(repo):
    root, _ = repo
    subprocess.run(["git", "-C", str(root), "mv", "README.md", "READ.md"], check=True)
    files = mcp.dirty_files(root)
    assert "READ.md" in files
    assert "README.md" not in files


def test_task_restores_protected_test_files(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "cheat")
    root, _ = repo
    original = (root / "test_calc.py").read_text()
    out = run_task(repo, tmp_path)
    assert "WARNING: the worker changed protected files" in out
    assert "test_calc.py" in out.split("WARNING")[1].split("\n")[0]
    assert (root / "test_calc.py").read_text() == original
    assert (root / "denied.txt").read_text() == "Edit(test_calc.py)"


def test_task_test_files_listed_as_targets_are_not_protected(repo, tmp_path):
    root, _ = repo
    run_task(repo, tmp_path, files=["calc.py", "test_calc.py"])
    assert not (root / "denied.txt").exists()


def test_task_explicit_protect(repo, tmp_path):
    root, _ = repo
    out = run_task(repo, tmp_path, protect=["README.md"])
    assert "restored them: README.md" in out
    assert (root / "README.md").read_text() == "hello\n"


def test_task_missing_test_command(repo, tmp_path):
    with pytest.raises(mcp.ToolError, match="test command not found"):
        run_task(repo, tmp_path, test_command="no-such-binary-xyz -q")


def test_is_test_file():
    assert mcp.is_test_file("test_x.py")
    assert mcp.is_test_file("src/app.spec.ts")
    assert mcp.is_test_file("tests/helpers.py")
    assert not mcp.is_test_file("src/testing_utils.py")


def test_task_reports_refused_actions(repo, tmp_path):
    out = run_task(repo, tmp_path)
    assert "Refused actions" in out
    assert "Write: /etc/x" in out


def test_worker_has_no_unscoped_write_rules():
    # A bare Edit/Write allow rule lets the worker write outside the project.
    assert not {"Edit", "Write"} & set(mcp.WORKER_TOOLS)
