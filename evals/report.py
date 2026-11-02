"""Build evals/report/{results.md, tradeoff.png, per_class.png} from result CSVs.

    python -m evals.report --results evals/results/ [--split heldout] [--out evals/report/]

Uses the most recent batch (same timestamp) per gate for the chosen split; with several runs in
a batch, each cell shows the mean and the min-max spread.
"""
import argparse
import json
import math
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from evals import metrics as M  # noqa: E402
from evals.registry import REGISTRY  # noqa: E402
from tollgate.types import ATTACK_CLASSES  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
GATE_ORDER = list(REGISTRY)
# Reference categorical palette (dataviz skill), fixed slot per gate so color follows the entity.
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
INK, INK_2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"
LABELS = {"tollgate_offline": "tollgate_offline (heuristic stand-in)"}


def gate_color(name: str) -> str:
    i = GATE_ORDER.index(name) if name in GATE_ORDER else len(GATE_ORDER)
    return PALETTE[i % len(PALETTE)]


def load_batches(results_dir: Path, split: str | None) -> dict[str, list[tuple[dict, pd.DataFrame]]]:
    """gate -> [(sidecar, rows)] for the latest batch of that gate on the split."""
    found: dict[tuple[str, str], dict[str, list]] = {}
    for meta_path in sorted(results_dir.glob("*.json")):
        csv_path = meta_path.with_suffix(".csv")
        if not csv_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text())
        except json.JSONDecodeError:
            continue
        if "gate" not in meta or "timestamp" not in meta:
            continue
        if split and meta.get("split") != split:
            continue
        df = pd.read_csv(csv_path, keep_default_na=False, na_values=[])
        df["attack_class"] = df["attack_class"].replace("", None)
        found.setdefault((meta["gate"], meta.get("split")), {}).setdefault(meta["timestamp"], []).append((meta, df))
    out = {}
    for (gate, _), batches in found.items():
        latest = batches[max(batches)]
        out[gate] = sorted(latest, key=lambda md: md[0].get("run", 0))
    return dict(sorted(out.items(), key=lambda kv: GATE_ORDER.index(kv[0]) if kv[0] in GATE_ORDER else 99))


def fmt(stat: dict, kind: str) -> str:
    mean, lo, hi = stat["mean"], stat["min"], stat["max"]
    if mean is None or (isinstance(mean, float) and math.isnan(mean)):
        return "n/a"
    f = {"rate": lambda x: f"{100 * x:.1f}%", "ms": lambda x: f"{x:,.2f}", "usd": lambda x: f"{x:,.2f}",
         "count": lambda x: f"{x:.0f}"}[kind]
    s = f(mean)
    if stat["runs"] > 1 and f(lo) != f(hi):
        s += f" ({f(lo)}–{f(hi)})"
    return s


def results_table(batches) -> tuple[str, dict]:
    head = ["Gate", "Runs", "N"] + [label for _, label, _ in M.METRICS]
    lines = ["| " + " | ".join(head) + " |", "|" + "|".join(["---"] + ["---:"] * (len(head) - 1)) + "|"]
    summary = {}
    for gate, runs in batches.items():
        agg = M.aggregate([M.compute_metrics(df) for _, df in runs])
        summary[gate] = agg
        name = f"`{gate}`" + (" *" if runs[0][0].get("offline_standin") else "")
        cells = [name, str(len(runs)), str(int(agg["n"]["mean"]))]
        cells += [fmt(agg[k], kind) for k, _, kind in M.METRICS]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines), summary


def _style(ax):
    ax.set_facecolor(SURFACE)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(INK_2)
        ax.spines[s].set_linewidth(0.8)
    ax.tick_params(colors=INK_2, labelsize=9)
    ax.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def plot_tradeoff(batches, path: Path, split: str):
    fig, ax = plt.subplots(figsize=(7.2, 5.2), dpi=150, facecolor=SURFACE)
    _style(ax)
    for gate, runs in batches.items():
        sweeps = [M.threshold_sweep(df) for _, df in runs]
        fb = np.nanmean([s["false_block_rate"].to_numpy() for s in sweeps], axis=0) * 100
        catch = np.nanmean([s["catch_rate"].to_numpy() for s in sweeps], axis=0) * 100
        pts = sorted(set(zip(fb.round(4), catch.round(4))))  # distinct operating points, low -> high
        c = gate_color(gate)
        ax.plot([p[0] for p in pts], [p[1] for p in pts], color=c, linewidth=2, alpha=0.9,
                label=LABELS.get(gate, gate), zorder=3)
        agg = M.aggregate([M.compute_metrics(df) for _, df in runs])
        x, y = agg["false_block_rate"]["mean"] * 100, agg["catch_rate"]["mean"] * 100
        if not (math.isnan(x) or math.isnan(y)):
            ax.scatter([x], [y], s=60, color=c, edgecolor=SURFACE, linewidth=2, zorder=4)
    ax.set_xlim(-2, 102)
    ax.set_ylim(-2, 102)
    ax.set_xlabel("False-block rate on benign actions (%)", color=INK, fontsize=10)
    ax.set_ylabel("Attack catch rate (%)", color=INK, fontsize=10)
    ax.set_title(f"Catch vs false-block as the risk threshold sweeps 0 → 1 ({split})", color=INK, fontsize=11,
                 loc="left")
    ax.scatter([], [], s=60, color=INK_2, label="dot = actual verdicts (mean of runs)")
    leg = ax.legend(loc="lower right", frameon=False, fontsize=9)
    for t in leg.get_texts():
        t.set_color(INK)
    fig.tight_layout()
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)


def plot_per_class(batches, path: Path, split: str):
    gates = list(batches)
    classes = list(ATTACK_CLASSES)
    vals = {g: M.aggregate([M.per_class_catch(df) for _, df in runs]) for g, runs in batches.items()}
    fig, ax = plt.subplots(figsize=(10, 5.2), dpi=150, facecolor=SURFACE)
    _style(ax)
    ax.grid(True, axis="y", color=GRID, linewidth=0.8)
    ax.grid(False, axis="x")
    n = max(len(gates), 1)
    group_w = 0.8
    w = group_w / n
    x = np.arange(len(classes))
    for i, g in enumerate(gates):
        ys = [vals[g][c]["mean"] * 100 if not math.isnan(vals[g][c]["mean"]) else 0.0 for c in classes]
        missing = [math.isnan(vals[g][c]["mean"]) for c in classes]
        pos = x - group_w / 2 + w * (i + 0.5)
        ax.bar(pos, ys, width=w * 0.9, color=gate_color(g), label=LABELS.get(g, g), zorder=3)
        for p, y, m in zip(pos, ys, missing):
            if m or y == 0:  # make empty and zero bars visible
                ax.text(p, 0.8, "–" if m else "0", ha="center", va="bottom", color=INK_2, fontsize=8)
    ax.set_xticks(x)
    ax.set_xticklabels([c.replace("_", "\n") for c in classes], fontsize=9, color=INK_2)
    ax.set_ylim(0, 105)
    ax.set_ylabel("Catch rate (%)", color=INK, fontsize=10)
    ax.set_title(f"Per-class catch rate by gate ({split}; – = no items of that class)", color=INK, fontsize=11,
                 loc="left")
    leg = ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.16), ncol=min(n, 4), frameon=False, fontsize=9)
    for t in leg.get_texts():
        t.set_color(INK)
    fig.tight_layout()
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)


def build_report(results_dir: Path, out_dir: Path, split: str | None = "heldout") -> Path:
    batches = load_batches(results_dir, split)
    if not batches and split == "heldout":
        print("[report] no heldout results; using every split", file=sys.stderr)
        split = None
        batches = load_batches(results_dir, None)
    if not batches:
        raise SystemExit(f"no result CSV+JSON pairs in {results_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    table, _ = results_table(batches)
    split_name = split or "all splits"
    metas = [runs[0][0] for runs in batches.values()]
    notes = []
    if any(m.get("offline_standin") for m in metas):
        notes.append("\\* `tollgate_offline` is the Tollgate engine with the offline keyword heuristic in place "
                     "of Jev. It is a development stand-in, **not the real system**; do not quote its numbers as "
                     "Tollgate's.")
    if any(m.get("fixture_data") for m in metas):
        notes.append("**Fixture data**: these runs used the 20-item test fixture in `evals/tests/fixtures/`, not "
                     "the real dataset. Numbers only show the harness works.")
    notes.append("Cells show the mean across runs, with the min–max spread in brackets when runs differ. "
                 "Catch and auto-deny are over attacks; false-block is over `benign` (excluding hard benign); "
                 "hard-benign false-block is over `hard_benign`; escalation, latency and cost are over all items.")
    provenance = ["| Gate | Batch (UTC) | Models | Policy hash | Git | Data |", "|---|---|---|---|---|---|"]
    for gate, runs in batches.items():
        m = runs[0][0]
        mv = ", ".join(f"{k}={v}" for k, v in m.get("model_versions", {}).items() if k != "system")
        provenance.append(f"| `{gate}` | {m['timestamp']} | {mv or '–'} | {m.get('policy_file_hash') or '–'} | "
                          f"{m.get('git_commit') or '–'} | {m.get('data_dir')} |")
    md = [f"# Tollgate eval results ({split_name})", "", table, "", *[f"{n}\n" for n in notes],
          "![Catch vs false-block](tradeoff.png)", "", "![Per-class catch rate](per_class.png)", "",
          "## Provenance", "", *provenance, ""]
    path = out_dir / "results.md"
    path.write_text("\n".join(md))
    plot_tradeoff(batches, out_dir / "tradeoff.png", split_name)
    plot_per_class(batches, out_dir / "per_class.png", split_name)
    print(f"[report] wrote {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}, tradeoff.png, "
          f"per_class.png ({len(batches)} gates)")
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", default=str(ROOT / "evals" / "results"))
    ap.add_argument("--out", default=str(ROOT / "evals" / "report"))
    ap.add_argument("--split", default="heldout", help="heldout | calibration | all | any (every split)")
    a = ap.parse_args(argv)
    build_report(Path(a.results), Path(a.out), None if a.split == "any" else a.split)
    return 0


if __name__ == "__main__":
    sys.exit(main())
