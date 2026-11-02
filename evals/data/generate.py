"""Deterministic dataset generator for Tollgate.

    .venv/bin/python -m evals.data.generate

Writes attacks.jsonl, benign.jsonl, hard_benign.jsonl, splits.json and SAMPLE.md into evals/data/.
No model calls: hand-written seeds (evals/data/prompts/handwritten.py) plus hand-written templates
(evals/data/prompts/{attacks,benign,hard_benign}.py) instantiated with a fixed seed.
"""
from __future__ import annotations

import json
import random
from collections import defaultdict
from pathlib import Path

from tollgate.types import Action, LabeledAction

from evals.data.prompts import attacks, benign, handwritten, hard_benign
from evals.data.prompts.world import Gen

SEED = 20261028
CALIBRATION_FRACTION = 0.30
DATA_DIR = Path(__file__).resolve().parent
PREFIX = {"attack": "atk", "benign": "ben", "hard_benign": "hb"}
FILES = {"attack": "attacks.jsonl", "benign": "benign.jsonl", "hard_benign": "hard_benign.jsonl"}


def _content_key(d: dict) -> str:
    keep = {k: d[k] for k in ("tool", "args", "operator_request", "recent_steps", "documents")}
    return json.dumps(keep, sort_keys=True, default=str)


def _collect(g: Gen) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {"attack": [], "benign": [], "hard_benign": []}
    seen: set[str] = set()

    def add(subset, cls, d, source):
        d = dict(d, subset=subset, attack_class=cls, source=source)
        seen.add(_content_key(d))
        out[subset].append(d)

    for subset, items in (("attack", handwritten.ATTACKS), ("benign", handwritten.BENIGN), ("hard_benign", handwritten.HARD_BENIGN)):
        for d in items:
            add(subset, d["attack_class"], d, "handwritten")

    jobs = [("attack", cls, tid, fn, n) for tid, cls, fn, n in attacks.TEMPLATES]
    jobs += [("benign", None, tid, fn, n) for tid, fn, n in benign.TEMPLATES]
    jobs += [("hard_benign", None, tid, fn, n) for tid, fn, n in hard_benign.TEMPLATES]
    for subset, cls, tid, fn, n in jobs:
        made = 0
        tries = 0
        while made < n:
            tries += 1
            if tries > n * 20:
                raise RuntimeError(f"template {tid} keeps producing duplicates")
            d = fn(g)
            k = _content_key(d)
            if k in seen:
                continue
            add(subset, cls, d, f"templated:{tid}")
            made += 1
    return out


def _to_labeled(d: dict, action_id: str) -> LabeledAction:
    action = Action(action_id=action_id, tool=d["tool"], args=d["args"], operator_request=d["operator_request"],
                    recent_steps=list(d["recent_steps"]), documents=[x for x in d["documents"] if x],
                    vendor_context=d["vendor_context"], amount_usd=d["amount_usd"])
    is_attack = d["subset"] == "attack"
    return LabeledAction(action=action, label="attack" if is_attack else "benign", subset=d["subset"],
                         attack_class=d["attack_class"], expected=d["expected"], rationale=d["rationale"], source=d["source"])


def make_splits(items: list[LabeledAction], rng: random.Random) -> dict[str, list[str]]:
    strata: dict[tuple, list[str]] = defaultdict(list)
    for it in items:
        strata[(it.subset, it.attack_class)].append(it.action.action_id)
    cal, held = [], []
    for key in sorted(strata, key=str):
        ids = sorted(strata[key])
        rng.shuffle(ids)
        k = round(len(ids) * CALIBRATION_FRACTION)
        cal += ids[:k]
        held += ids[k:]
    return {"calibration": sorted(cal), "heldout": sorted(held)}


def _fmt_item(it: LabeledAction) -> str:
    a = it.action
    lines = [f"### {a.action_id} — {it.subset}" + (f" / {it.attack_class}" if it.attack_class else ""),
             f"- **expected:** `{it.expected}`  |  **source:** `{it.source}`",
             f"- **rationale:** {it.rationale}",
             f"- **operator_request:** {a.operator_request}",
             f"- **proposed call:** `{a.tool}`" + (f"  |  amount_usd: ${a.amount_usd:,.2f}" if a.amount_usd is not None else ""),
             "", "```json", json.dumps(a.args, indent=2), "```", "", "**recent_steps:**", ""]
    lines += [f"{i}. `{s}`" for i, s in enumerate(a.recent_steps, 1)]
    if a.vendor_context:
        vc = a.vendor_context
        lines += ["", f"**vendor_context:** {vc.vendor_id} {vc.name}, record {vc.record_age_days}d old, "
                      f"last bank change {vc.last_bank_change_days_ago if vc.last_bank_change_days_ago is not None else 'never'}"
                      f"{' (verified)' if vc.bank_change_verified else ''}, typical "
                      f"{'$' + format(vc.typical_payment_usd, ',.2f') if vc.typical_payment_usd is not None else 'n/a'}"]
    for i, doc in enumerate(a.documents, 1):
        lines += ["", f"**document {i}:**", "", "```text", doc, "```"]
    return "\n".join(lines) + "\n"


def write_sample(by_subset: dict[str, list[LabeledAction]], rng: random.Random, path: Path) -> None:
    picks = rng.sample(by_subset["attack"], 10) + rng.sample(by_subset["benign"], 5) + rng.sample(by_subset["hard_benign"], 5)
    head = ["# Dataset sample for human review", "",
            "20 random items (10 attack, 5 benign, 5 hard benign), regenerated by `python -m evals.data.generate`.",
            "Check: is each attack realistic and correctly labeled? Would a careful AP clerk allow each benign item?", ""]
    path.write_text("\n".join(head) + "\n" + "\n---\n\n".join(_fmt_item(p) for p in picks))


def main(out_dir: Path = DATA_DIR) -> None:
    out_dir = Path(out_dir)
    rng = random.Random(SEED)
    raw = _collect(Gen(rng))
    order_rng = random.Random(SEED + 1)
    by_subset: dict[str, list[LabeledAction]] = {}
    for subset, items in raw.items():
        order_rng.shuffle(items)  # ids do not reveal template or class
        labeled = [_to_labeled(d, f"{PREFIX[subset]}-{i:04d}") for i, d in enumerate(items, 1)]
        by_subset[subset] = labeled
        with open(out_dir / FILES[subset], "w") as f:
            for it in labeled:
                f.write(it.model_dump_json() + "\n")
    all_items = [x for v in by_subset.values() for x in v]
    splits = make_splits(all_items, random.Random(SEED + 2))
    (out_dir / "splits.json").write_text(json.dumps(splits, indent=1) + "\n")
    write_sample(by_subset, random.Random(SEED + 3), out_dir / "SAMPLE.md")
    print({k: len(v) for k, v in by_subset.items()}, "calibration", len(splits["calibration"]), "heldout", len(splits["heldout"]))


if __name__ == "__main__":
    main()
