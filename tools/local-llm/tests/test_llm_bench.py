import importlib.util
import json
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "llm_bench", Path(__file__).parent.parent / "llm_bench.py"
)
lb = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = lb
_spec.loader.exec_module(lb)

MANIFEST = """# tier\trole\trepo\tfile\tbytes\tsha256
1\tprimary\torg/Big-GGUF\tBig-Q4_K_M.gguf\t100\tabc
1\tembed\torg/Emb-GGUF\tEmb-Q8_0.gguf\t10\tdef
"""


def result(n_prompt, n_gen, depth, avg, sd=0.5):
    return {"n_prompt": n_prompt, "n_gen": n_gen, "n_depth": depth, "avg_ts": avg, "stddev_ts": sd}


@pytest.mark.parametrize(
    ("r", "expected"),
    [
        (result(4096, 0, 0, 1), "pp4096"),
        (result(0, 128, 0, 1), "tg128"),
        (result(4096, 0, 16384, 1), "pp4096@16K"),
        (result(0, 128, 1000, 1), "tg128@1000"),
    ],
)
def test_label(r, expected):
    assert lb.label(r) == expected


def test_read_manifest_skips_comments(tmp_path):
    (tmp_path / "m.tsv").write_text(MANIFEST)
    models = lb.read_manifest(tmp_path / "m.tsv")
    assert [m.name for m in models] == ["Big-Q4_K_M", "Emb-Q8_0"]
    assert models[0].path(Path("/srv")) == Path("/srv/org/Big-GGUF/Big-Q4_K_M.gguf")


def test_summarize_builds_table_and_reports_errors():
    rows = [
        {
            "model": "Big",
            "backend": "rocm",
            "results": [result(4096, 0, 0, 1344.6), result(0, 128, 0, 73.65)],
        },
        {"model": "Big", "backend": "vulkan", "error": "out of memory"},
    ]
    table = lb.summarize(rows)
    assert "| Model | Backend | pp4096 | tg128 |" in table
    assert "| Big | rocm | 1344.6 ± 0.5 | 73.7 ± 0.5 |" in table
    assert "failed: out of memory" in table


def test_bench_command_uses_serving_settings(tmp_path):
    args = lb.build_parser().parse_args(["speed"])
    cmd = lb.bench_command(Path("/bin/llama-bench"), tmp_path / "m.gguf", args)
    joined = " ".join(cmd)
    assert "-fa on" in joined
    assert "-ctk q8_0 -ctv q8_0" in joined
    assert "-d 0,16384" in joined
    assert "-o json" in joined


def test_speed_dry_run_lists_only_downloaded_matching_models(tmp_path, capsys):
    (tmp_path / "m.tsv").write_text(MANIFEST)
    models = tmp_path / "models"
    (models / "org/Big-GGUF").mkdir(parents=True)
    (models / "org/Big-GGUF/Big-Q4_K_M.gguf").write_bytes(b"x")
    rc = lb.main(
        ["speed", "--dry-run", "--backends", "vulkan", "--manifest", str(tmp_path / "m.tsv"),
         "--models-dir", str(models), "--out-dir", str(tmp_path / "out")]
    )  # fmt: skip
    assert rc == 0
    out = capsys.readouterr().out
    assert "Big-Q4_K_M.gguf" in out
    assert "build-vulkan/bin/llama-bench" in out
    assert "Emb" not in out  # embed role excluded by default


def test_speed_with_nothing_downloaded_fails(tmp_path):
    (tmp_path / "m.tsv").write_text(MANIFEST)
    rc = lb.main(
        ["speed", "--manifest", str(tmp_path / "m.tsv"), "--models-dir", str(tmp_path / "none")]
    )
    assert rc == 1


def test_report_round_trips(tmp_path, capsys):
    path = tmp_path / "r.jsonl"
    row = {"model": "M", "backend": "rocm", "results": [result(0, 128, 0, 50)]}
    path.write_text(json.dumps(row))
    assert lb.main(["report", str(path)]) == 0
    assert "| M | rocm | 50.0 ± 0.5 |" in capsys.readouterr().out
