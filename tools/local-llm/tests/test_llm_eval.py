import importlib.util
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "llm_eval", Path(__file__).parent.parent / "llm_eval.py"
)
le = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = le
_spec.loader.exec_module(le)


class FakeClient:
    """Returns canned chat answers; scripted responses for /v1/messages."""

    def __init__(self, answer: str = "", script: list[dict] | None = None) -> None:
        self.answer = answer
        self.script = list(script or [])

    def chat(self, prompt, **_):
        return self.answer

    def messages(self, payload):
        return self.script.pop(0)


GOOD_TESTS = """```python
import pytest
from duration import parse_duration

def test_values():
    assert parse_duration("1h30m") == 5400
    assert parse_duration("10m5s") == 605
    assert parse_duration("45s") == 45

@pytest.mark.parametrize("bad", ["", "abc", "1x"])
def test_invalid(bad):
    with pytest.raises(ValueError):
        parse_duration(bad)
```"""

GOOD_IMPL = """```python
import re

def merge_intervals(intervals):
    out = []
    for a, b in sorted(intervals):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out

def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
```"""


def test_extract_code_prefers_python_block():
    text = "intro\n```bash\nls\n```\n```python\nx = 1\n```"
    assert le.extract_code(text) == "x = 1\n"


def test_extract_code_falls_back_to_text():
    assert le.extract_code("x = 1") == "x = 1"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"a": 1}', {"a": 1}),
        ('Sure:\n```json\n{"a": 1}\n```', {"a": 1}),
        ("no json here", None),
        ("{broken", None),
    ],
)
def test_extract_json(text, expected):
    assert le.extract_json(text) == expected


@pytest.mark.parametrize(
    ("task", "good", "bad"),
    [
        (le.task_log_root_cause, "The db pool hit max_connections=20.", "Disk full."),
        (le.task_diff_bug, "Off-by-one: size - 1 drops the last item.", "Looks fine."),
        (le.task_write_tests, GOOD_TESTS, "```python\ndef test_x():\n    assert True\n```"),
        (le.task_implement, GOOD_IMPL, "```python\ndef merge_intervals(x):\n    return x\n```"),
        (
            le.task_json_extract,
            '{"invoice_id": "INV-2291", "vendor": "Northwind Traders", '
            '"amount": 4812.5, "due_date": "2026-11-14"}',
            '{"invoice_id": "INV-2291", "amount": "4,812.50"}',
        ),
    ],
)
def test_task_grading(task, good, bad):
    assert task(FakeClient(good)).passed
    assert not task(FakeClient(bad)).passed


def test_mutants_differ_from_reference():
    assert all(m != le.DURATION_IMPL for m in le.DURATION_MUTANTS)


def tool_use(name, args, id_="t1"):
    return {"content": [{"type": "tool_use", "id": id_, "name": name, "input": args}]}


FIXED = le.AGENT_REPO["stats.py"].replace(
    "    return ordered[mid]\n", "    return (ordered[mid - 1] + ordered[mid]) / 2\n"
)
FIXED = FIXED.replace(
    "    if len(ordered) % 2:\n        return (ordered[mid - 1] + ordered[mid]) / 2\n",
    "    if len(ordered) % 2:\n        return ordered[mid]\n",
)


def test_agentic_task_passes_with_correct_fix():
    script = [
        tool_use("read_file", {"path": "stats.py"}),
        tool_use("write_file", {"path": "stats.py", "content": FIXED}),
        tool_use("run_tests", {}),
        {"content": [{"type": "text", "text": "Fixed median for even lengths."}]},
    ]
    outcome = le.task_agentic_fix(FakeClient(script=script))
    assert outcome.passed, outcome.detail


def test_agentic_task_fails_if_tests_edited():
    script = [
        tool_use("write_file", {"path": "test_stats.py", "content": "def test_ok():\n    pass\n"}),
        {"content": [{"type": "text", "text": "done"}]},
    ]
    assert not le.task_agentic_fix(FakeClient(script=script)).passed


def test_agentic_task_fails_without_fix():
    script = [{"content": [{"type": "text", "text": "I can't help."}]}]
    assert not le.task_agentic_fix(FakeClient(script=script)).passed


def test_sandbox_blocks_path_traversal(tmp_path):
    box = le.Sandbox(tmp_path)
    assert box.execute("read_file", {"path": "../../etc/passwd"}).startswith("error")
    assert box.execute("write_file", {"path": "/etc/x", "content": "y"}).startswith("error")
    assert box.execute("nope", {}).startswith("error")


def test_evaluate_counts_exceptions_as_failures():
    def boom(_client):
        raise RuntimeError("server died")

    rows = le.evaluate(FakeClient(), [le.Task("boom", boom)], repeats=2)
    assert [r["passed"] for r in rows] == [False, False]
    assert "server died" in rows[0]["detail"]


def test_summarize():
    rows = [
        {"model": "A", "task": "t1", "passed": True, "seconds": 1.0},
        {"model": "A", "task": "t1", "passed": False, "seconds": 2.0},
        {"model": "B", "task": "t1", "passed": True, "seconds": 3.0},
    ]
    table = le.summarize(rows)
    assert "| A | 1/2 | 1/2 | 3 |" in table
    assert "| B | 1/1 | 1/1 | 3 |" in table


def test_first_failure_picks_assertion_line():
    out = "..F\n    def test_x():\nE   assert 3 == 4\nFAILED test_a.py::test_x\n"
    assert le.first_failure(out) == "E   assert 3 == 4"


def test_write_tests_reports_why_correct_impl_failed():
    wrong = (
        "```python\nfrom duration import parse_duration\n\n"
        "def test_x():\n    assert parse_duration('1m') == 61\n```"
    )
    outcome = le.task_write_tests(FakeClient(wrong))
    assert not outcome.passed
    assert "60 == 61" in outcome.detail


def test_tasks_option_does_not_swallow_models():
    args = le.build_parser().parse_args(
        ["run", "--tasks", "diff_bug,implement", "a.gguf", "b.gguf"]
    )
    assert args.tasks == ["diff_bug", "implement"]
    assert args.models == ["a.gguf", "b.gguf"]


def test_tasks_option_rejects_unknown():
    with pytest.raises(SystemExit):
        le.build_parser().parse_args(["run", "--tasks", "nope", "a.gguf"])
