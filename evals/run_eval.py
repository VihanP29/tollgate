"""Run gates over a labeled split and write one CSV (+ sidecar JSON) per run.

    python -m evals.run_eval --gate rules --split heldout --runs 2
    python -m evals.run_eval --gate rules,passthrough,tollgate_offline --split heldout
    python -m evals.run_eval --gate llm_judge --split heldout --live        # paid API calls

Output: evals/results/<gate>_<split>_<run>_<timestamp>.csv and a matching .json sidecar.
Data: evals/data/*.jsonl + splits.json. If missing/incomplete, falls back to the 20-item fixture
in evals/tests/fixtures/ (loudly). --data points at another directory.
"""
import argparse
import csv
import datetime as dt
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

from evals.registry import REGISTRY, GateUnavailable, load_gate, model_versions
from tollgate.types import LabeledAction

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "evals" / "data"
FIXTURE_DIR = ROOT / "evals" / "tests" / "fixtures"
RESULTS_DIR = ROOT / "evals" / "results"
DATA_FILES = ("attacks.jsonl", "benign.jsonl", "hard_benign.jsonl", "public.jsonl")
REQUIRED_FILES = ("attacks.jsonl", "benign.jsonl", "hard_benign.jsonl", "splits.json")
COLUMNS = ["action_id", "label", "subset", "attack_class", "expected", "verdict", "risk", "latency_ms", "cost_usd",
           "reasons"]
SPLITS = ("calibration", "heldout", "all")


def data_is_complete(data_dir: Path) -> tuple[bool, str]:
    missing = [f for f in REQUIRED_FILES if not (data_dir / f).exists() or (data_dir / f).stat().st_size == 0]
    if missing:
        return False, f"missing {', '.join(missing)}"
    try:
        splits = json.loads((data_dir / "splits.json").read_text())
        if not splits.get("calibration") or not splits.get("heldout"):
            return False, "splits.json has an empty split"
    except (json.JSONDecodeError, AttributeError) as e:
        return False, f"splits.json unreadable ({e})"
    return True, "ok"


def resolve_data_dir(data: str | None) -> Path:
    if data:
        d = Path(data)
        ok, why = data_is_complete(d)
        if not ok:
            raise SystemExit(f"--data {d}: {why}")
        return d
    ok, why = data_is_complete(DATA_DIR)
    if ok:
        return DATA_DIR
    print(f"[run_eval] WARNING: evals/data is not ready ({why}); using the 20-item FIXTURE in "
          f"{FIXTURE_DIR.relative_to(ROOT)}. Numbers are for harness testing only.", file=sys.stderr)
    return FIXTURE_DIR


def load_items(data_dir: Path, split: str) -> list[LabeledAction]:
    items: dict[str, LabeledAction] = {}
    for fname in DATA_FILES:
        f = data_dir / fname
        if not f.exists():
            continue
        for n, line in enumerate(f.read_text().splitlines(), 1):
            if not line.strip():
                continue
            it = LabeledAction.model_validate_json(line)
            if it.action.action_id in items:
                raise SystemExit(f"duplicate action_id {it.action.action_id} ({fname}:{n})")
            items[it.action.action_id] = it
    if split == "all":
        return list(items.values())
    ids = json.loads((data_dir / "splits.json").read_text())[split]
    unknown = [i for i in ids if i not in items]
    if unknown:
        print(f"[run_eval] WARNING: {len(unknown)} ids in splits.json[{split}] not found in data, skipped "
              f"(e.g. {unknown[:3]})", file=sys.stderr)
    return [items[i] for i in ids if i in items]


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def git_commit() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None if out.returncode == 0 else None
    except Exception:
        return None


def policy_hash() -> str | None:
    try:
        from tollgate.compiler import load_policies
        return load_policies().file_hash
    except Exception:
        return None


def run_gate(gate, items: list[LabeledAction]) -> tuple[list[dict], int]:
    rows, errors = [], 0
    for idx, it in enumerate(items):
        t0 = time.perf_counter()
        try:
            r = gate.decide(it.action)
            verdict, risk, reasons, lat, cost = r.verdict, r.risk, r.reasons, r.latency_ms, r.cost_usd
        except Exception as e:
            if idx == 0:
                raise GateUnavailable(f"{getattr(gate, 'name', '?')}: first action failed ({type(e).__name__}: {e})")
            errors += 1
            verdict, risk, reasons = "escalate", 0.5, [f"gate_error: {type(e).__name__}: {str(e)[:200]}"]
            lat, cost = (time.perf_counter() - t0) * 1000, 0.0
        rows.append({
            "action_id": it.action.action_id, "label": it.label, "subset": it.subset,
            "attack_class": it.attack_class or "", "expected": it.expected, "verdict": verdict,
            "risk": round(float(risk), 4), "latency_ms": round(float(lat), 3), "cost_usd": round(float(cost), 8),
            "reasons": " | ".join(reasons),
        })
    return rows, errors


def write_run(out_dir: Path, gate_name: str, split: str, run: int, stamp: str, rows: list[dict], meta: dict) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = out_dir / f"{gate_name}_{split}_{run}_{stamp}"
    with open(stem.with_suffix(".csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    stem.with_suffix(".json").write_text(json.dumps(meta, indent=2) + "\n")
    return stem.with_suffix(".csv")


def evaluate(gate_name: str, split: str, runs: int, data_dir: Path, out_dir: Path, live: bool,
             limit: int | None = None, gate=None) -> list[Path]:
    """Run one gate. Raises GateUnavailable if it cannot run."""
    if split not in SPLITS:
        raise SystemExit(f"unknown split {split}")
    gate = gate or load_gate(gate_name, live=live)
    items = load_items(data_dir, split)
    if limit:
        items = items[:limit]
    if not items:
        raise SystemExit(f"no items in split {split}")
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    spec = REGISTRY.get(gate_name)
    data_files = {f: file_sha(data_dir / f) for f in (*DATA_FILES, "splits.json") if (data_dir / f).exists()}
    paths = []
    for run in range(1, runs + 1):
        if hasattr(gate, "parse_errors"):
            gate.parse_errors = 0
        t0 = time.perf_counter()
        rows, errors = run_gate(gate, items)
        meta = {
            "gate": gate_name,
            "description": spec.description if spec else None,
            "offline_standin": gate_name == "tollgate_offline",
            "model_versions": model_versions(gate),
            "policy_file_hash": policy_hash(),
            "git_commit": git_commit(),
            "timestamp": stamp,
            "run": run,
            "runs_in_batch": runs,
            "split": split,
            "n_items": len(rows),
            "data_dir": str(data_dir.relative_to(ROOT)) if data_dir.is_relative_to(ROOT) else str(data_dir),
            "fixture_data": data_dir.resolve() == FIXTURE_DIR.resolve(),
            "data_file_hashes": data_files,
            "live": live,
            "gate_errors": errors,
            "judge_parse_errors": getattr(gate, "parse_errors", None),
            "wall_s": round(time.perf_counter() - t0, 3),
        }
        p = write_run(out_dir, gate_name, split, run, stamp, rows, meta)
        caught = sum(r["verdict"] != "allow" for r in rows if r["label"] == "attack")
        n_atk = sum(r["label"] == "attack" for r in rows)
        print(f"[run_eval] {gate_name} {split} run {run}/{runs}: {len(rows)} items, "
              f"caught {caught}/{n_atk} attacks, {errors} errors -> {p.relative_to(ROOT) if p.is_relative_to(ROOT) else p}")
        paths.append(p)
    return paths


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gate", required=True, help=f"comma list or 'all' ({', '.join(REGISTRY)})")
    ap.add_argument("--split", default="heldout", choices=SPLITS)
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--data", help="dataset directory (default evals/data, fixture fallback)")
    ap.add_argument("--out", default=str(RESULTS_DIR))
    ap.add_argument("--live", action="store_true", help="allow paid/network gates (llm_judge, tollgate)")
    ap.add_argument("--limit", type=int, help="only the first N items (smoke tests)")
    a = ap.parse_args(argv)

    names = list(REGISTRY) if a.gate == "all" else [g.strip() for g in a.gate.split(",") if g.strip()]
    data_dir = resolve_data_dir(a.data)
    ran = 0
    for name in names:
        try:
            evaluate(name, a.split, a.runs, data_dir, Path(a.out), a.live, a.limit)
            ran += 1
        except GateUnavailable as e:
            print(f"[run_eval] SKIP {e}", file=sys.stderr)
    return 0 if ran else 1


if __name__ == "__main__":
    sys.exit(main())
