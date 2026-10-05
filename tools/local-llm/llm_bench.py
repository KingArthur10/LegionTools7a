#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Benchmark local models across llama.cpp backends on this machine.

`speed` runs llama-bench for every downloaded model in models.tsv (roles
primary/helper by default) on each backend, with the settings we intend to serve
with (flash attention, q8_0 KV cache, all layers on GPU). Tests mirror our
workloads: a 4K-token prompt and 128 generated tokens, both on an empty context
and with 16K tokens already in context (roughly a Claude Code session).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger("llm-bench")

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "models.tsv"
LLAMA_CPP_DIR = Path(
    os.environ.get("LLAMA_CPP_DIR", Path.home() / ".cache/legiontools7a/llama.cpp")
)
MODELS_DIR = Path(os.environ.get("LLM_MODELS_DIR", "/srv/llm/models"))
BACKENDS = {
    "rocm": {"ROCBLAS_USE_HIPBLASLT": "1"},
    "vulkan": {},
}


@dataclass(frozen=True)
class Model:
    tier: str
    role: str
    repo: str
    file: str

    @property
    def name(self) -> str:
        return self.file.removesuffix(".gguf")

    def path(self, models_dir: Path) -> Path:
        return models_dir / self.repo / self.file


def read_manifest(path: Path) -> list[Model]:
    models = []
    for line in path.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        tier, role, repo, file, *_ = line.split("\t")
        models.append(Model(tier, role, repo, file))
    return models


def bench_command(binary: Path, model: Path, args: argparse.Namespace) -> list[str]:
    return [
        str(binary),
        "-m", str(model),
        "-ngl", "99",
        "-fa", "on",
        "-ctk", args.cache_type,
        "-ctv", args.cache_type,
        "-p", str(args.prompt),
        "-n", str(args.gen),
        "-d", ",".join(str(d) for d in args.depths),
        "-r", str(args.reps),
        "-o", "json",
    ]  # fmt: skip


def label(result: dict) -> str:
    """Short test name like 'pp4096', 'tg128@16K'."""
    name = f"pp{result['n_prompt']}" if result["n_prompt"] else f"tg{result['n_gen']}"
    depth = result.get("n_depth", 0)
    if depth:
        name += f"@{depth // 1024}K" if depth % 1024 == 0 else f"@{depth}"
    return name


def summarize(rows: list[dict]) -> str:
    """Markdown table: one row per model x backend, one column per test."""
    tests: list[str] = []
    table: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        key = (row["model"], row["backend"])
        cells = table.setdefault(key, {})
        if "error" in row:
            cells["error"] = row["error"]
            continue
        for result in row["results"]:
            test = label(result)
            if test not in tests:
                tests.append(test)
            cells[test] = f"{result['avg_ts']:.1f} ± {result['stddev_ts']:.1f}"
    header = ["Model", "Backend", *tests]
    lines = ["| " + " | ".join(header) + " |", "|" + " --- |" * len(header)]
    for (model, backend), cells in table.items():
        if "error" in cells:
            values = [f"failed: {cells['error']}"] + [""] * (len(tests) - 1)
        else:
            values = [cells.get(t, "") for t in tests]
        lines.append("| " + " | ".join([model, backend, *values]) + " |")
    return "\n".join(lines) + "\n\nValues are tokens/second (mean ± stddev).\n"


def run_speed(args: argparse.Namespace) -> int:
    models = [
        m
        for m in read_manifest(args.manifest)
        if m.role in args.roles and m.path(args.models_dir).exists()
        and (not args.model or any(s.lower() in m.name.lower() for s in args.model))
    ]  # fmt: skip
    if not models:
        log.error("no matching downloaded models in %s", args.models_dir)
        return 1

    args.out_dir.mkdir(parents=True, exist_ok=True)
    out = args.out_dir / f"speed-{datetime.now():%Y%m%d-%H%M%S}.jsonl"
    rows = []
    for model in models:
        for backend in args.backends:
            binary = args.llama_cpp_dir / f"build-{backend}" / "bin" / "llama-bench"
            cmd = bench_command(binary, model.path(args.models_dir), args)
            log.info("%s on %s", model.name, backend)
            if args.dry_run:
                print(" ".join(cmd))
                continue
            env = {**os.environ, **BACKENDS[backend]}
            proc = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
            row = {"model": model.name, "role": model.role, "backend": backend}
            if proc.returncode == 0:
                row["results"] = json.loads(proc.stdout)
            else:
                row["error"] = (proc.stderr.strip().splitlines() or ["unknown"])[-1][:120]
                log.warning("failed: %s", row["error"])
            rows.append(row)
            with out.open("a") as f:
                f.write(json.dumps(row) + "\n")
    if rows:
        print(summarize(rows))
        log.info("raw results: %s", out)
    return 0


def run_report(args: argparse.Namespace) -> int:
    rows = [json.loads(line) for line in args.results.read_text().splitlines() if line]
    print(summarize(rows))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="llm-bench", description=__doc__.splitlines()[0])
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("speed", help="llama-bench every model on every backend")
    p.add_argument("--backends", nargs="+", choices=BACKENDS, default=list(BACKENDS))
    p.add_argument("--roles", nargs="+", default=["primary", "helper"])
    p.add_argument("--model", nargs="+", help="only models whose name contains this")
    p.add_argument("--prompt", type=int, default=4096, help="prompt tokens (default: 4096)")
    p.add_argument("--gen", type=int, default=128, help="generated tokens (default: 128)")
    p.add_argument(
        "--depths", type=int, nargs="+", default=[0, 16384], help="context already filled"
    )
    p.add_argument("--reps", type=int, default=3, help="repetitions (default: 3)")
    p.add_argument("--cache-type", default="q8_0", help="KV cache type (default: q8_0)")
    p.add_argument("--dry-run", action="store_true", help="print commands only")
    p.add_argument("--manifest", type=Path, default=MANIFEST, help=argparse.SUPPRESS)
    p.add_argument("--models-dir", type=Path, default=MODELS_DIR, help=argparse.SUPPRESS)
    p.add_argument("--llama-cpp-dir", type=Path, default=LLAMA_CPP_DIR, help=argparse.SUPPRESS)
    p.add_argument("--out-dir", type=Path, default=HERE / "results", help="raw JSONL output")

    r = sub.add_parser("report", help="re-print the summary of a results file")
    r.add_argument("results", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s: %(message)s"
    )
    return {"speed": run_speed, "report": run_report}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
